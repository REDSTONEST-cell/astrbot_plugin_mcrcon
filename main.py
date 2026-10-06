import asyncio
import json
import struct
import os
import re
import time
import aiosqlite
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.event.filter import EventMessageType, PlatformAdapterType
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.api.star import StarTools
from astrbot.api import AstrBotConfig  # 配置管理


class AsyncRcon:  # 异步RCON类
    def __init__(self, host: str, port: int, password: str):
        self.host = host
        self.port = port
        self.password = password
        self.reader = None
        self.writer = None

    async def connect(self):
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        await self._send_packet(0, 3, self.password)  # 登录
        await self._recv_packet()

    async def send_cmd(self, command: str) -> str:
        await self._send_packet(1, 2, command)
        _, _, body = await self._recv_packet()
        return body

    async def close(self):
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()

    async def _send_packet(self, req_id: int, ptype: int, payload: str):
        data = struct.pack("<ii", req_id, ptype) + payload.encode() + b"\x00\x00"
        length = struct.pack("<i", len(data))
        self.writer.write(length + data)
        await self.writer.drain()

    async def _recv_packet(self):
        length_bytes = await self.reader.readexactly(4)
        length = struct.unpack("<i", length_bytes)[0]
        data = await self.reader.readexactly(length)
        req_id, ptype = struct.unpack("<ii", data[:8])
        body = data[8:].rstrip(b"\x00").decode(errors="ignore")
        return req_id, ptype, body


def strip_mc_color(text: str) -> str:
    return re.sub(r"§.", "", text)


async def rcon_command(
    host: str, port: int, password: str, command: str
) -> str:  # 执行rcon命令
    """统一执行任意 RCON 命令"""
    rcon = AsyncRcon(host, port, password)
    await rcon.connect()
    try:
        return await rcon.send_cmd(command)
    finally:
        await rcon.close()


class BindingStore:  # QQ 与 MC 账号绑定记录 (SQLite)
    def __init__(self, db_path: str):
        self.db_path = db_path

    async def init(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                """
                CREATE TABLE IF NOT EXISTS bindings (
                    qq TEXT PRIMARY KEY,
                    mcname TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    group_id TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL
                )
                """
            )
            await db.commit()

    async def get_by_qq(self, qq: str) -> str | None:
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT mcname FROM bindings WHERE qq = ?", (qq,)
            ) as cur:
                row = await cur.fetchone()
        return row[0] if row else None

    async def get_by_mcname(self, mcname: str) -> tuple[str, str, int] | None:
        """返回 (qq, group_id, created_at)，MC 名不区分大小写"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT qq, group_id, created_at FROM bindings WHERE mcname = ?",
                (mcname,),
            ) as cur:
                row = await cur.fetchone()
        return tuple(row) if row else None

    async def add(self, qq: str, mcname: str, group_id: str = "") -> bool:
        """新增绑定，QQ 或 MC 名已存在时返回 False"""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(
                    "INSERT INTO bindings (qq, mcname, group_id, created_at)"
                    " VALUES (?, ?, ?, ?)",
                    (qq, mcname, group_id, int(time.time())),
                )
                await db.commit()
            return True
        except aiosqlite.IntegrityError:
            return False

    async def remove_by_qq(self, qq: str) -> str | None:
        """删除绑定，返回被解绑的 MC 名"""
        async with aiosqlite.connect(self.db_path) as db:
            async with db.execute(
                "SELECT mcname FROM bindings WHERE qq = ?", (qq,)
            ) as cur:
                row = await cur.fetchone()
            if not row:
                return None
            await db.execute("DELETE FROM bindings WHERE qq = ?", (qq,))
            await db.commit()
        return row[0]


@register(
    "astrbot_plugin_mcman", "卡带酱", "一个基于RCON协议的MC服务器管理器插件", "1.2.0"
)
class MyPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.whitelist_command = self.config.get("whitelist_command", "whitelist")
        self.admin_qqs = set(self.config.get("admin_qqs", []))
        self.rcon_host = self.config.get("rcon_host")
        self.rcon_port = self.config.get("rcon_port")
        self.rcon_password = self.config.get("rcon_password")
        # 申请白名单功能
        self.enable_apply_whitelist = self.config.get("enable_apply_whitelist", False)
        # 退群自动解绑
        self.unbind_on_leave = self.config.get("unbind_on_leave", True)
        self.unbind_groups = {str(g) for g in self.config.get("unbind_groups", [])}
        self.unbind_remove_whitelist = self.config.get("unbind_remove_whitelist", True)
        self.plugin_data_dir = StarTools.get_data_dir("astrbot_plugin_mcman")
        self.apply_file = os.path.join(self.plugin_data_dir, "apply_whitelist.json")
        self.store = BindingStore(os.path.join(self.plugin_data_dir, "bindings.db"))

    async def initialize(self):
        await self.store.init()
        await self._migrate_json()
        logger.info("mcman plugin by kdj")

    async def _migrate_json(self):
        """把旧版 apply_whitelist.json 中的绑定导入 SQLite，导入后重命名旧文件"""
        if not os.path.exists(self.apply_file):
            return
        with open(self.apply_file, "r", encoding="utf-8") as f:
            old_data = json.load(f)
        for qq, mcname in old_data.items():
            if not await self.store.add(str(qq), mcname):
                logger.warning(f"迁移跳过冲突的绑定: {qq} -> {mcname}")
        os.replace(self.apply_file, self.apply_file + ".migrated")
        logger.info(f"已将 {len(old_data)} 条旧绑定记录迁移到 SQLite")

    def is_admin(self, qqid: str) -> bool:
        return qqid in self.admin_qqs

    async def execute_and_reply(self, event: AstrMessageEvent, command: str, desc: str):
        """通用执行 + 回复逻辑"""
        user_name = event.get_sender_name()
        sender_qq = str(event.get_sender_id())
        named = f"{user_name}({sender_qq})"

        try:
            resp = await rcon_command(
                self.rcon_host, self.rcon_port, self.rcon_password, command
            )
            cresp = strip_mc_color(resp)
            logger.info(f"RCON 执行结果: {resp}")
            yield event.plain_result(
                f"你好, {named}, 已尝试执行 `{command}` ({desc})\n\n服务器返回：\n{cresp}"
            )
        except Exception as e:
            logger.error(f"RCON 执行失败: {e}")
            yield event.plain_result(f"你好, {named}, 操作失败：{e}")

    @filter.command("mcwl", desc="MC 白名单管理", alias={"mcwhitelist"})
    async def mcwl(self, event: AstrMessageEvent, o: str, mcname: str = ""):
        if not self.is_admin(str(event.get_sender_id())):
            yield event.plain_result("抱歉，你没有权限执行此操作。")
            return
        command = f"{self.whitelist_command} {o} {mcname}".strip()
        async for msg in self.execute_and_reply(event, command, "白名单管理"):
            yield msg

    @filter.command("mcban", desc="MC 黑名单添加")
    async def mcban(self, event: AstrMessageEvent, mcname: str = "", reason: str = ""):
        if not self.is_admin(str(event.get_sender_id())):
            yield event.plain_result("抱歉，你没有权限执行此操作。")
            return
        command = f"ban {mcname} {reason}".strip()
        async for msg in self.execute_and_reply(event, command, "黑名单添加"):
            yield msg

    @filter.command("mcpardon", desc="MC 黑名单移除", alias={"mcunban"})
    async def mcpardon(self, event: AstrMessageEvent, mcname: str = ""):
        if not self.is_admin(str(event.get_sender_id())):
            yield event.plain_result("抱歉，你没有权限执行此操作。")
            return
        command = f"pardon {mcname}".strip()
        async for msg in self.execute_and_reply(event, command, "黑名单移除"):
            yield msg

    @filter.command("mcbanlist", desc="MC 黑名单查看", alias={"mcbl"})
    async def mcbl(self, event: AstrMessageEvent):
        async for msg in self.execute_and_reply(event, "banlist", "查看黑名单"):
            yield msg

    @filter.command("mclist", desc="MC 查看在线玩家", alias={"mcl"})
    async def mclist(self, event: AstrMessageEvent):
        async for msg in self.execute_and_reply(event, "list", "查看在线玩家"):
            yield msg

    @filter.command("mckick", desc="MC 踢出指定玩家", alias={"mck"})
    async def mckick(self, event: AstrMessageEvent, mcname: str = "", reason: str = ""):
        if not self.is_admin(str(event.get_sender_id())):
            yield event.plain_result("抱歉，你没有权限执行此操作。")
            return
        command = f"kick {mcname} {reason}".strip()
        async for msg in self.execute_and_reply(event, command, "踢出玩家"):
            yield msg

    @filter.command("mctempban", desc="MC 临时黑名单", alias={"mctb"})
    async def mctempban(
        self,
        event: AstrMessageEvent,
        mcname: str = "",
        time: str = "",
        reason: str = "",
    ):
        if not self.is_admin(str(event.get_sender_id())):
            yield event.plain_result("抱歉，你没有权限执行此操作。")
            return
        command = f"tempban {mcname} {time} {reason}".strip()
        async for msg in self.execute_and_reply(event, command, "临时封禁"):
            yield msg

    @filter.command("mcsay", desc="MC 说话", alias={"mcs"})
    async def mcsay(self, event: AstrMessageEvent, text: str = ""):
        user_name = event.get_sender_name()
        sender_qq = str(event.get_sender_id())
        named = f"{user_name}({sender_qq})"

        if not text:
            yield event.plain_result(f"你好, {named}, 请输入信息!")
            return

        message = [
            {"text": f"(QQ消息) ", "color": "aqua"},
            {"text": f"<{named}>", "color": "green", "underlined": True},
            {"text": " 说: ", "color": "white"},
            {"text": text, "color": "yellow"},
        ]
        command = f"tellraw @a {json.dumps(message, ensure_ascii=False)}"
        async for msg in self.execute_and_reply(event, command, "玩家发言"):
            yield msg

    @filter.command("mcbroadcast", desc="MC 广播消息", alias={"mcb", "mcbc"})
    async def mcbroadcast(self, event: AstrMessageEvent, text: str = ""):
        user_name = event.get_sender_name()
        sender_qq = str(event.get_sender_id())
        named = f"{user_name}({sender_qq})"
        if not self.is_admin(str(event.get_sender_id())):
            yield event.plain_result("抱歉，你没有权限执行此操作。")
            return
        if not text:
            yield event.plain_result(f"你好, {named}, 请输入广播信息!")
            return

        message = [
            {"text": f"<管理员广播消息>", "color": "green", "underlined": True},
            {"text": " ", "color": "white", "underlined": False},
            {"text": text, "color": "yellow", "underlined": False},
        ]
        command = f"tellraw @a {json.dumps(message, ensure_ascii=False)}"
        async for msg in self.execute_and_reply(event, command, "广播消息"):
            yield msg

    @filter.command("wantwl", desc="申请MC白名单")
    async def wantwl(self, event: AstrMessageEvent, mcname: str = ""):
        if not self.enable_apply_whitelist:
            yield event.plain_result("抱歉，白名单申请功能未开启。")
            return
        if not mcname:
            yield event.plain_result("请输入要绑定的MC用户名。")
            return

        qqid = str(event.get_sender_id())
        bound = await self.store.get_by_qq(qqid)
        if bound:
            yield event.plain_result(f"你已经绑定过MC账号 `{bound}`，不能重复申请。")
            return
        if await self.store.get_by_mcname(mcname):
            yield event.plain_result(f"MC账号 `{mcname}` 已被其他QQ绑定。")
            return

        # 调用RCON执行
        command = f"{self.whitelist_command} add {mcname}"
        try:
            resp = await rcon_command(
                self.rcon_host, self.rcon_port, self.rcon_password, command
            )
            if not await self.store.add(qqid, mcname, str(event.get_group_id() or "")):
                yield event.plain_result(
                    f"绑定失败：MC账号 `{mcname}` 或你的QQ已被绑定。"
                )
                return
            yield event.plain_result(
                f"成功为你绑定MC账号 `{mcname}` 并加入白名单！\n服务器返回：{strip_mc_color(resp)}"
            )
        except Exception as e:
            yield event.plain_result(f"申请失败：{e}")

    @filter.command("mckill", desc="MC kill人")
    async def mckill(self, event: AstrMessageEvent, mcname: str = ""):
        if not self.is_admin(str(event.get_sender_id())):
            yield event.plain_result("抱歉，你没有权限执行此操作。")
            return
        command = f"kill {mcname}".strip()
        async for msg in self.execute_and_reply(event, command, "kill"):
            yield msg

    @filter.command("mcplugins", desc="MC 插件列表")
    async def mcplugins(
        self,
        event: AstrMessageEvent,
    ):
        command = f"plugins".strip()
        async for msg in self.execute_and_reply(event, command, "插件列表"):
            yield msg

    @filter.command("mcwho", desc="MC 玩家名反查绑定的QQ")
    async def mcwho(self, event: AstrMessageEvent, mcname: str = ""):
        if not self.is_admin(str(event.get_sender_id())):
            yield event.plain_result("抱歉，你没有权限执行此操作。")
            return
        if not mcname:
            yield event.plain_result("请输入要查询的MC用户名。")
            return
        row = await self.store.get_by_mcname(mcname)
        if not row:
            yield event.plain_result(f"MC账号 {mcname} 没有绑定任何QQ。")
            return
        qq, group_id, created_at = row
        bound_time = time.strftime("%Y-%m-%d %H:%M", time.localtime(created_at))
        group_info = f"，申请群 {group_id}" if group_id else ""
        yield event.plain_result(
            f"MC账号 {mcname} 绑定的QQ：{qq}\n绑定时间：{bound_time}{group_info}"
        )

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_decrease(self, event: AstrMessageEvent):
        """监听 OneBot 群成员减少通知，退群/被踢时自动解绑"""
        raw = event.message_obj.raw_message
        if (
            not self.unbind_on_leave
            or not isinstance(raw, dict)
            or raw.get("post_type") != "notice"
            or raw.get("notice_type") != "group_decrease"
            or raw.get("sub_type") == "kick_me"  # 机器人自己被踢
        ):
            return
        group_id = str(raw.get("group_id", ""))
        if self.unbind_groups and group_id not in self.unbind_groups:
            return

        qqid = str(raw.get("user_id", ""))
        mcname = await self.store.remove_by_qq(qqid)
        if not mcname:
            return
        logger.info(f"QQ {qqid} 退出群 {group_id}，已解绑 MC 账号 {mcname}")

        msg = f"QQ {qqid} 已退群，已自动解绑MC账号 {mcname}"
        if self.unbind_remove_whitelist:
            try:
                await rcon_command(
                    self.rcon_host,
                    self.rcon_port,
                    self.rcon_password,
                    f"{self.whitelist_command} remove {mcname}",
                )
                msg += " 并移出白名单"
            except Exception as e:
                logger.error(f"退群移出白名单失败: {e}")
                msg += f"，但移出白名单失败：{e}"
        yield event.plain_result(msg)

    async def terminate(self):
        logger.info("mcman plugin stopped")
