import aiosqlite
import calendar
from datetime import date, datetime, timedelta
import os
import re
import asyncio
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger
from astrbot.core.star import StarTools
from .resources.deer_core import DeerCore
from .resources.klittra_core import KlittraCore

FONT_FILE = "font.ttf"
DEER_DB_NAME = "deer_checkin.db"
KLITTRA_DB_NAME = "klittra_checkin.db"

# 🤏 表情可带肤色修饰符（U+1F3FB ~ U+1F3FF）或可选 VS16（U+FE0F），匹配/统计时需一并支持
KLITTRA_EMOJI_RE = r"🤏[\U0001F3FB-\U0001F3FF\uFE0F]*"


class DeerCheckinPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config if config is not None else {}

        # 配置项
        self.group_whitelist = self.config.get("group_whitelist", [])
        self.user_blacklist = self.config.get("user_blacklist", [])
        self.day_start_time = self.config.get("day_start_time", "00:00")
        self.auto_delete_last_month_data = bool(
            self.config.get("auto_delete_last_month_data", False)
        )
        self.daily_max_checkins = int(self.config.get("daily_max_checkins", 0))
        self.monthly_max_checkins = int(self.config.get("monthly_max_checkins", 0))
        self.enable_female_calendar = bool(
            self.config.get("enable_female_calendar", False)
        )
        self.ranking_display_count = int(self.config.get("ranking_display_count", 10))

        data_dir = StarTools.get_data_dir("astrbot_plugin_deer_check")
        os.makedirs(data_dir, exist_ok=True)
        plugin_dir = os.path.dirname(__file__)
        resources_dir = os.path.join(plugin_dir, "resources")
        self.deer_db_path = os.path.join(data_dir, DEER_DB_NAME)
        self.klittra_db_path = os.path.join(data_dir, KLITTRA_DB_NAME)
        self.font_path = os.path.join(resources_dir, FONT_FILE)
        self.temp_dir = os.path.join(data_dir, "tmp")
        os.makedirs(self.temp_dir, exist_ok=True)

        self.deer_core = DeerCore(self.font_path, self.deer_db_path, self.temp_dir)
        self.klittra_core = KlittraCore(
            self.font_path, self.klittra_db_path, self.temp_dir
        )

        self._initialized = False
        self._init_lock = asyncio.Lock()

    async def _ensure_initialized(self):
        """确保数据库和月度清理只在首次调用时异步执行一次"""
        async with self._init_lock:
            if not self._initialized:
                await self._init_db()
                await self._monthly_cleanup()
                self._initialized = True

    async def _init_db(self):
        """初始化数据库和表结构"""
        try:
            # 初始化鹿打卡数据库
            async with aiosqlite.connect(self.deer_db_path) as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS checkin (
                        user_id TEXT NOT NULL,
                        checkin_date TEXT NOT NULL,
                        deer_count INTEGER NOT NULL,
                        PRIMARY KEY (user_id, checkin_date)
                    )
                """)
                # 为"今日首鹿"功能补充首次打卡时间列（兼容旧库）
                cursor = await conn.execute("PRAGMA table_info(checkin)")
                columns = [row[1] for row in await cursor.fetchall()]
                if "created_at" not in columns:
                    await conn.execute("ALTER TABLE checkin ADD COLUMN created_at TEXT")
                await conn.commit()
            logger.info("鹿打卡数据库初始化成功。")

            # 初始化扣日历数据库
            async with aiosqlite.connect(self.klittra_db_path) as conn:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS klittra_checkin (
                        user_id TEXT NOT NULL,
                        checkin_date TEXT NOT NULL,
                        klittra_count INTEGER NOT NULL,
                        PRIMARY KEY (user_id, checkin_date)
                    )
                """)
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )
                """)
                await conn.commit()
            logger.info("扣日历数据库初始化成功。")
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}")

    def _get_adjusted_date(self, current_time: datetime) -> str:
        """根据配置的 day_start_time 获取调整后的日期字符串 (YYYY-MM-DD)"""
        # 解析HH:MM格式的时间
        try:
            hour, minute = map(int, self.day_start_time.split(":"))
            day_start_time = current_time.replace(
                hour=hour, minute=minute, second=0, microsecond=0
            )
        except (ValueError, AttributeError):
            # 如果格式不正确，默认使用00:00
            day_start_time = current_time.replace(
                hour=0, minute=0, second=0, microsecond=0
            )

        # 如果当前时间小于设置的每天开始时间，则认为是前一天
        if current_time.time() < day_start_time.time():
            adjusted_date = current_time - timedelta(days=1)
        else:
            adjusted_date = current_time
        return adjusted_date.strftime("%Y-%m-%d")

    def _parse_period_param(self, param: str) -> tuple:
        """
        解析月份/年份参数（🦌报告/🦌排行/🦌日历 共用）。
        返回 (kind, year, month, err_msg)：
          kind == 'month' → 目标月份，year/month 为实际年份与月份（月份>当前月则为去年）
          kind == 'year'  → 目标年份，month 为 0
          kind == None    → 参数非法，err_msg 为可直接发送的提示
        """
        current_year = datetime.now().year
        current_month = datetime.now().month
        if len(param) == 4:
            try:
                target_year = int(param)
            except ValueError:
                return None, 0, 0, "请输入正确的年份数字！"
            if target_year > current_year:
                return None, 0, 0, "年份不能超过当前年份哦！"
            return "year", target_year, 0, ""
        try:
            target_month = int(param)
        except ValueError:
            return None, 0, 0, "请输入正确的月份数字！"
        if not (1 <= target_month <= 12):
            return None, 0, 0, "月份必须在1-12之间哦！"
        if target_month > current_month:
            target_year = current_year - 1
        else:
            target_year = current_year
        return "month", target_year, target_month, ""

    def _period_name(self, is_month: bool, year: int, month: int) -> str:
        """返回周期的展示文案：当前月/今年用'本月'/'今年'，否则用具体的'X年X月'/'X年'。"""
        now = datetime.now()
        if is_month:
            if year == now.year and month == now.month:
                return "本月"
            return f"{year}年{month}月"
        if year == now.year:
            return "今年"
        return f"{year}年"

    async def _monthly_cleanup(self):
        """检查是否进入新月份，如果是则清空旧数据（根据配置决定）"""
        current_time = datetime.now()
        adjusted_date_str = self._get_adjusted_date(current_time)
        current_month = adjusted_date_str[:7]  # YYYY-MM format
        try:
            # 清理鹿打卡数据库
            async with aiosqlite.connect(self.deer_db_path) as conn:
                cursor = await conn.execute(
                    "SELECT value FROM metadata WHERE key = 'last_cleanup_month'"
                )
                last_cleanup = await cursor.fetchone()

                if not last_cleanup or last_cleanup[0] != current_month:
                    # 根据配置决定是否删除上月数据
                    if self.auto_delete_last_month_data:
                        await conn.execute(
                            "DELETE FROM checkin WHERE strftime('%Y-%m', checkin_date) != ?",
                            (current_month,),
                        )
                        logger.info(
                            f"已执行月度清理，删除了鹿打卡数据中非 {current_month} 的数据。"
                        )

                    await conn.execute(
                        "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                        ("last_cleanup_month", current_month),
                    )
                    await conn.commit()

            # 清理扣日历数据库
            async with aiosqlite.connect(self.klittra_db_path) as conn:
                cursor = await conn.execute(
                    "SELECT value FROM metadata WHERE key = 'last_cleanup_month_klittra'"
                )
                last_cleanup = await cursor.fetchone()

                if not last_cleanup or last_cleanup[0] != current_month:
                    # 根据配置决定是否删除上月数据
                    if self.auto_delete_last_month_data:
                        await conn.execute(
                            "DELETE FROM klittra_checkin WHERE strftime('%Y-%m', checkin_date) != ?",
                            (current_month,),
                        )
                        logger.info(
                            f"已执行月度清理，删除了扣日历数据中非 {current_month} 的数据。"
                        )

                    await conn.execute(
                        "INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                        ("last_cleanup_month_klittra", current_month),
                    )
                    await conn.commit()
        except Exception as e:
            logger.error(f"月度数据清理失败: {e}")

    async def _guard(self, event: AstrMessageEvent, require_group: bool = False):
        """权限守卫：返回 (group_id, user_id)；被白名单/黑名单/私聊限制拦截时返回 None。"""
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if (
            self.group_whitelist
            and group_id
            and int(group_id) not in self.group_whitelist
        ):
            logger.info(f"群 {group_id} 不在白名单中，忽略请求")
            return None

        if user_id in self.user_blacklist:
            logger.info(f"用户 {user_id} 在黑名单中，忽略请求")
            return None

        if require_group and not group_id:
            return None

        await self._ensure_initialized()
        return group_id, user_id

    @filter.regex(r"^🦌+$")
    async def handle_deer_checkin(self, event: AstrMessageEvent):
        """处理鹿打卡事件：记录数据，然后发送日历。"""
        guarded = await self._guard(event)
        if not guarded:
            return
        group_id, user_id = guarded
        user_name = event.get_sender_name()
        deer_count = event.message_str.count("🦌")

        current_time = datetime.now()
        today_str = self._get_adjusted_date(current_time)

        # 检查每日和每月计入次数限制
        if self.daily_max_checkins > 0 or self.monthly_max_checkins > 0:
            # 查询当前日期和当前月份的打卡次数
            async with aiosqlite.connect(self.deer_db_path) as conn:
                # 查询当日打卡次数
                if self.daily_max_checkins > 0:
                    cursor = await conn.execute(
                        """
                        SELECT deer_count FROM checkin WHERE user_id = ? AND checkin_date = ?
                    """,
                        (user_id, today_str),
                    )
                    today_record = await cursor.fetchone()

                    current_daily_count = today_record[0] if today_record else 0
                    new_daily_count = current_daily_count + deer_count

                    if new_daily_count > self.daily_max_checkins:
                        yield event.plain_result(
                            f"打卡失败！今日计入次数已达上限 {self.daily_max_checkins} 次。"
                        )
                        return

                # 查询当月打卡次数
                if self.monthly_max_checkins > 0:
                    current_month = today_str[:7]  # YYYY-MM 格式
                    # 查询本月其他日期的总次数
                    cursor = await conn.execute(
                        """
                        SELECT SUM(deer_count) FROM checkin
                        WHERE user_id = ? AND strftime('%Y-%m', checkin_date) = ? AND checkin_date != ?
                    """,
                        (user_id, current_month, today_str),
                    )
                    monthly_record = await cursor.fetchone()

                    current_monthly_count = (
                        monthly_record[0]
                        if monthly_record and monthly_record[0] is not None
                        else 0
                    )

                    # 查询当天已有的数量
                    cursor = await conn.execute(
                        """
                        SELECT deer_count FROM checkin WHERE user_id = ? AND checkin_date = ?
                    """,
                        (user_id, today_str),
                    )
                    today_record = await cursor.fetchone()
                    existing_count = (
                        today_record[0]
                        if today_record and today_record[0] is not None
                        else 0
                    )

                    # 计算打卡后的总数
                    new_monthly_count = (
                        current_monthly_count + existing_count + deer_count
                    )

                    if new_monthly_count > self.monthly_max_checkins:
                        yield event.plain_result(
                            f"打卡失败！本月计入次数已达上限 {self.monthly_max_checkins} 次。"
                        )
                        return

        try:
            async with aiosqlite.connect(self.deer_db_path) as conn:
                await conn.execute(
                    """
                    INSERT INTO checkin (user_id, checkin_date, deer_count, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id, checkin_date)
                    DO UPDATE SET deer_count = deer_count + excluded.deer_count;
                """,
                    (
                        user_id,
                        today_str,
                        deer_count,
                        current_time.strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                await conn.commit()
            logger.info(
                f"用户 {user_name} ({user_id}) 打卡成功，记录了 {deer_count} 个🦌。"
            )
        except Exception as e:
            logger.error(f"记录用户 {user_name} ({user_id}) 的打卡数据失败: {e}")
            yield event.plain_result("打卡失败，数据库出错了 >_<")
            return

        async for result in self._generate_and_send_calendar(event, today_str):
            yield result

    @filter.regex(rf"^(?:{KLITTRA_EMOJI_RE})+$")
    async def handle_klittra_checkin(self, event: AstrMessageEvent):
        """处理扣日历记录事件：如果启用了扣日历功能，则记录数据并发送扣日历。"""
        # 检查是否启用了扣日历功能
        if not self.enable_female_calendar:
            return  # 未启用扣日历功能，不处理

        guarded = await self._guard(event)
        if not guarded:
            return
        group_id, user_id = guarded
        user_name = event.get_sender_name()
        pinch_count = event.message_str.count("🤏")

        current_time = datetime.now()
        today_str = self._get_adjusted_date(current_time)

        # 检查每日和每月计入次数限制（复用 deer 的限制配置）
        if self.daily_max_checkins > 0 or self.monthly_max_checkins > 0:
            # 查询当前日期和当前月份的打卡次数
            async with aiosqlite.connect(self.klittra_db_path) as conn:
                # 查询当日打卡次数
                if self.daily_max_checkins > 0:
                    cursor = await conn.execute(
                        """
                        SELECT klittra_count FROM klittra_checkin WHERE user_id = ? AND checkin_date = ?
                    """,
                        (user_id, today_str),
                    )
                    today_record = await cursor.fetchone()

                    current_daily_count = today_record[0] if today_record else 0
                    new_daily_count = current_daily_count + pinch_count

                    if new_daily_count > self.daily_max_checkins:
                        yield event.plain_result(
                            f"记录失败！今日计入次数已达上限 {self.daily_max_checkins} 次。"
                        )
                        return

                # 查询当月打卡次数
                if self.monthly_max_checkins > 0:
                    current_month = today_str[:7]  # YYYY-MM 格式
                    # 查询本月其他日期的总次数
                    cursor = await conn.execute(
                        """
                        SELECT SUM(klittra_count) FROM klittra_checkin
                        WHERE user_id = ? AND strftime('%Y-%m', checkin_date) = ? AND checkin_date != ?
                    """,
                        (user_id, current_month, today_str),
                    )
                    monthly_record = await cursor.fetchone()

                    current_monthly_count = (
                        monthly_record[0]
                        if monthly_record and monthly_record[0] is not None
                        else 0
                    )

                    # 查询当天已有的数量
                    cursor = await conn.execute(
                        """
                        SELECT klittra_count FROM klittra_checkin WHERE user_id = ? AND checkin_date = ?
                    """,
                        (user_id, today_str),
                    )
                    today_record = await cursor.fetchone()
                    existing_count = (
                        today_record[0]
                        if today_record and today_record[0] is not None
                        else 0
                    )

                    # 计算打卡后的总数
                    new_monthly_count = (
                        current_monthly_count + existing_count + pinch_count
                    )

                    if new_monthly_count > self.monthly_max_checkins:
                        yield event.plain_result(
                            f"记录失败！本月计入次数已达上限 {self.monthly_max_checkins} 次。"
                        )
                        return

        try:
            async with aiosqlite.connect(self.klittra_db_path) as conn:
                await conn.execute(
                    """
                    INSERT INTO klittra_checkin (user_id, checkin_date, klittra_count)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, checkin_date)
                    DO UPDATE SET klittra_count = klittra_count + excluded.klittra_count;
                """,
                    (user_id, today_str, pinch_count),
                )
                await conn.commit()
            logger.info(
                f"用户 {user_name} ({user_id}) 扣日历记录成功，记录了 {pinch_count} 个🤏。"
            )
        except Exception as e:
            logger.error(f"记录用户 {user_name} ({user_id}) 的扣日历数据失败: {e}")
            yield event.plain_result("扣日历记录失败，数据库出错了 >_<")
            return

        # 发送扣日历
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()

        (
            result_text,
            image_path,
            has_error,
        ) = await self.klittra_core._generate_and_send_klittra_calendar(
            event, user_id, user_name, self.klittra_db_path, today_str
        )

        if result_text:
            yield event.plain_result(result_text)
            if has_error:
                return

        if image_path:
            yield event.image_result(image_path)

        # 删除临时图片文件
        if image_path and os.path.exists(image_path):
            try:
                await asyncio.to_thread(os.remove, image_path)
                logger.debug(f"已成功删除临时图片: {image_path}")
            except OSError as e:
                logger.error(f"删除临时图片 {image_path} 失败: {e}")

    @filter.regex(r"^🦌日历(?:\s+(\d{1,2}))?$")
    async def handle_calendar_command(self, event: AstrMessageEvent):
        """'🦌日历' 命令：查看打卡日历。不带参数查本月；🦌日历 11 查11月。"""
        guarded = await self._guard(event)
        if not guarded:
            return
        group_id, user_id = guarded
        user_name = event.get_sender_name()

        match = re.search(r"^🦌日历(?:\s+(\d{1,2}))?$", event.message_str)
        param = match.group(1) if match and match.group(1) else None

        if param is None:
            current_time = datetime.now()
            today_str = self._get_adjusted_date(current_time)
            async for result in self._generate_and_send_calendar(event, today_str):
                yield result
            return

        kind, target_year, target_month, err = self._parse_period_param(param)
        if kind is None:
            yield event.plain_result(err)
            return

        logger.info(
            f"用户 {user_name} ({user_id}) 请求查看 {target_year}年{target_month}月的日历。"
        )
        async for result in self._generate_and_send_calendar(
            event, f"{target_year}-{target_month:02d}-01"
        ):
            yield result

    @filter.regex(rf"^{KLITTRA_EMOJI_RE}日历$")
    async def handle_klittra_calendar_command(self, event: AstrMessageEvent):
        """'🤏日历' 命令，只查询并发送用户的当月扣日历。"""
        # 检查是否启用了扣日历功能
        if not self.enable_female_calendar:
            return  # 未启用扣日历功能，不处理

        guarded = await self._guard(event)
        if not guarded:
            return
        group_id, user_id = guarded
        user_name = event.get_sender_name()
        current_time = datetime.now()
        adjusted_date_str = self._get_adjusted_date(current_time)
        current_year = int(adjusted_date_str[:4])
        current_month = int(adjusted_date_str[5:7])
        current_month_str = adjusted_date_str[:7]

        checkin_records = {}
        total_deer_this_month = 0
        try:
            async with aiosqlite.connect(self.klittra_db_path) as conn:
                async with conn.execute(
                    "SELECT checkin_date, klittra_count FROM klittra_checkin WHERE user_id = ? AND strftime('%Y-%m', checkin_date) = ?",
                    (user_id, current_month_str),
                ) as cursor:
                    rows = await cursor.fetchall()
                    if not rows:
                        yield event.plain_result(
                            "您本月还没有扣日历记录哦，发送“🤏”开始第一次记录吧！"
                        )
                        return

                    for row in rows:
                        day = int(row[0].split("-")[2])
                        count = row[1]
                        checkin_records[day] = count
                        total_deer_this_month += count
        except Exception as e:
            logger.error(f"查询用户 {user_name} ({user_id}) 的扣日历月度数据失败: {e}")
            yield event.plain_result("查询扣日历数据时出错了 >_<")
            return

        image_path = ""
        try:
            image_path = await asyncio.to_thread(
                self.klittra_core._create_klittra_calendar_image,
                user_id,
                user_name,
                current_year,
                current_month,
                checkin_records,
                total_deer_this_month,
            )
            yield event.image_result(image_path)
        except FileNotFoundError:
            logger.error("字体文件未找到！无法生成扣日历图片。")
            yield event.plain_result(
                f"服务器缺少字体文件，无法生成扣日历图片。本月您已扣了{len(checkin_records)}天，累计{total_deer_this_month}次。"
            )
        except Exception as e:
            logger.error(f"生成或发送扣日历图片失败: {e}")
            yield event.plain_result("处理扣日历图片时发生了未知错误 >_<")
        finally:
            if image_path and os.path.exists(image_path):
                try:
                    await asyncio.to_thread(os.remove, image_path)
                    logger.debug(f"已成功删除临时图片: {image_path}")
                except OSError as e:
                    logger.error(f"删除临时图片 {image_path} 失败: {e}")

    @filter.regex(r"^🦌补签\s+(\d{1,2})(?:\s+(\d+))?\s*$")
    async def handle_retro_checkin(self, event: AstrMessageEvent):
        """
        处理补签命令，格式: '🦌补签 <日期> [次数]'
        """
        guarded = await self._guard(event)
        if not guarded:
            return
        group_id, user_id = guarded

        # 在函数内部，对消息原文进行正则搜索
        pattern = r"^🦌补签\s+(\d{1,2})(?:\s+(\d+))?\s*$"
        match = re.search(pattern, event.message_str)

        if not match:
            logger.error("补签处理器被触发，但内部正则匹配失败！这不应该发生。")
            return

        user_name = event.get_sender_name()

        # 从 match 对象中解析日期和次数
        try:
            day_str, count_str = match.groups()
            day_to_checkin = int(day_str)
            deer_count = int(count_str) if count_str else 1
            if deer_count <= 0:
                yield event.plain_result("补签次数必须是大于0的整数哦！")
                return
        except (ValueError, TypeError):
            yield event.plain_result(
                "命令格式不正确，请使用：🦌补签 日期 [次数] (例如：🦌补签 1 5 或 🦌补签 1)"
            )
            return

        # 验证日期有效性
        current_time = datetime.now()
        adjusted_date_str = self._get_adjusted_date(current_time)
        adjusted_date = datetime.strptime(adjusted_date_str, "%Y-%m-%d").date()
        current_year = adjusted_date.year
        current_month = adjusted_date.month

        days_in_month = calendar.monthrange(current_year, current_month)[1]

        if not (1 <= day_to_checkin <= days_in_month):
            yield event.plain_result(
                f"日期无效！本月（{current_month}月）只有 {days_in_month} 天。"
            )
            return

        if day_to_checkin > adjusted_date.day:
            yield event.plain_result("抱歉，不能对未来进行补签哦！")
            return

        # 添加补签日期并更新数据库
        target_date = date(current_year, current_month, day_to_checkin)
        target_date_str = target_date.strftime("%Y-%m-%d")

        # 检查每日和每月计入次数限制（针对补签日期）
        if self.daily_max_checkins > 0 or self.monthly_max_checkins > 0:
            # 查询当前日期和当前月份的打卡次数
            async with aiosqlite.connect(self.deer_db_path) as conn:
                # 查询当日打卡次数
                if self.daily_max_checkins > 0:
                    cursor = await conn.execute(
                        """
                        SELECT deer_count FROM checkin WHERE user_id = ? AND checkin_date = ?
                    """,
                        (user_id, target_date_str),
                    )
                    today_record = await cursor.fetchone()

                    current_daily_count = today_record[0] if today_record else 0
                    new_daily_count = current_daily_count + deer_count

                    if new_daily_count > self.daily_max_checkins:
                        yield event.plain_result(
                            f"补签失败！{target_date_str} 当日计入次数已达上限 {self.daily_max_checkins} 次。"
                        )
                        return

                # 查询当月打卡次数
                if self.monthly_max_checkins > 0:
                    current_month = target_date_str[:7]  # YYYY-MM 格式
                    # 查询本月其他日期的总次数
                    cursor = await conn.execute(
                        """
                        SELECT SUM(deer_count) FROM checkin
                        WHERE user_id = ? AND strftime('%Y-%m', checkin_date) = ? AND checkin_date != ?
                    """,
                        (user_id, current_month, target_date_str),
                    )
                    monthly_record = await cursor.fetchone()

                    current_monthly_count = (
                        monthly_record[0]
                        if monthly_record and monthly_record[0] is not None
                        else 0
                    )

                    # 查询目标日期已有的数量
                    cursor = await conn.execute(
                        """
                        SELECT deer_count FROM checkin WHERE user_id = ? AND checkin_date = ?
                    """,
                        (user_id, target_date_str),
                    )
                    today_record = await cursor.fetchone()
                    existing_count = (
                        today_record[0]
                        if today_record and today_record[0] is not None
                        else 0
                    )

                    # 计算补签后的总数
                    new_monthly_count = (
                        current_monthly_count + existing_count + deer_count
                    )

                    if new_monthly_count > self.monthly_max_checkins:
                        yield event.plain_result(
                            f"补签失败！本月计入次数已达上限 {self.monthly_max_checkins} 次。"
                        )
                        return

        try:
            async with aiosqlite.connect(self.deer_db_path) as conn:
                await conn.execute(
                    """
                    INSERT INTO checkin (user_id, checkin_date, deer_count, created_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id, checkin_date)
                    DO UPDATE SET deer_count = deer_count + excluded.deer_count;
                """,
                    (
                        user_id,
                        target_date_str,
                        deer_count,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
                await conn.commit()
            logger.info(
                f"用户 {user_name} ({user_id}) 成功为 {target_date_str} 补签了 {deer_count} 个🦌。"
            )
        except Exception as e:
            logger.error(f"为用户 {user_name} ({user_id}) 补签失败: {e}")
            yield event.plain_result("补签失败，数据库出错了 >_<")
            return

        # 发送成功提示，并返回更新后的日历图片
        yield event.plain_result(
            f"补签成功！已为 {current_month}月{day_to_checkin}日 增加了 {deer_count} 个鹿。"
        )
        current_time = datetime.now()
        adjusted_date_str = self._get_adjusted_date(current_time)
        async for result in self._generate_and_send_calendar(event, adjusted_date_str):
            yield result

    @filter.regex(r"^🦌撤销\s+(\d{1,2})(?:\s+(\d+))?\s*$")
    async def handle_undo_checkin(self, event: AstrMessageEvent):
        """
        处理撤销命令，格式: '🦌撤销 <日期> [次数]'
        """
        guarded = await self._guard(event)
        if not guarded:
            return
        group_id, user_id = guarded

        # 在函数内部，对消息原文进行正则搜索
        pattern = r"^🦌撤销\s+(\d{1,2})(?:\s+(\d+))?\s*$"
        match = re.search(pattern, event.message_str)

        if not match:
            logger.error("撤销处理器被触发，但内部正则匹配失败！这不应该发生。")
            return

        user_name = event.get_sender_name()

        # 从 match 对象中解析日期和次数
        try:
            day_str, count_str = match.groups()
            day_to_checkin = int(day_str)
            deer_count = int(count_str) if count_str else 1
            if deer_count <= 0:
                yield event.plain_result("撤销次数必须是大于0的整数哦！")
                return
        except (ValueError, TypeError):
            yield event.plain_result(
                "命令格式不正确，请使用：🦌撤销 日期 [次数] (例如：🦌撤销 1 5 或 🦌撤销 1)"
            )
            return

        # 验证日期有效性
        current_time = datetime.now()
        adjusted_date_str = self._get_adjusted_date(current_time)
        adjusted_date = datetime.strptime(adjusted_date_str, "%Y-%m-%d").date()
        current_year = adjusted_date.year
        current_month = adjusted_date.month

        days_in_month = calendar.monthrange(current_year, current_month)[1]

        if not (1 <= day_to_checkin <= days_in_month):
            yield event.plain_result(
                f"日期无效！本月（{current_month}月）只有 {days_in_month} 天。"
            )
            return

        if day_to_checkin > adjusted_date.day:
            yield event.plain_result("抱歉，不能对未来进行撤销哦！")
            return

        # 数据库操作
        target_date = date(current_year, current_month, day_to_checkin)
        target_date_str = target_date.strftime("%Y-%m-%d")

        try:
            async with aiosqlite.connect(self.deer_db_path) as conn:
                # 查询当前记录
                cursor = await conn.execute(
                    "SELECT deer_count FROM checkin WHERE user_id = ? AND checkin_date = ?",
                    (user_id, target_date_str),
                )
                record = await cursor.fetchone()

                if not record or record[0] < deer_count:
                    current_count = record[0] if record else 0
                    yield event.plain_result(
                        f"撤销失败！{target_date_str} 的打卡次数仅为 {current_count}，不足以减少 {deer_count} 次。"
                    )
                    return

                # 执行更新
                new_count = record[0] - deer_count
                if new_count == 0:
                    await conn.execute(
                        "DELETE FROM checkin WHERE user_id = ? AND checkin_date = ?",
                        (user_id, target_date_str),
                    )
                else:
                    await conn.execute(
                        "UPDATE checkin SET deer_count = ? WHERE user_id = ? AND checkin_date = ?",
                        (new_count, user_id, target_date_str),
                    )
                await conn.commit()

            logger.info(
                f"用户 {user_name} ({user_id}) 成功为 {target_date_str} 撤销了 {deer_count} 个🦌。"
            )
        except Exception as e:
            logger.error(f"为用户 {user_name} ({user_id}) 撤销失败: {e}")
            yield event.plain_result("撤销失败，数据库出错了 >_<")
            return

        # 发送成功提示，并返回更新后的日历图片
        yield event.plain_result(
            f"撤销成功！已为 {current_month}月{day_to_checkin}日 减少了 {deer_count} 个鹿。"
        )
        current_time = datetime.now()
        adjusted_date_str = self._get_adjusted_date(current_time)
        async for result in self._generate_and_send_calendar(event, adjusted_date_str):
            yield result

    @filter.regex(r"^🦌清空$")
    async def handle_clear_checkin(self, event: AstrMessageEvent):
        """
        响应 '🦌清空' 命令，清空当前用户的全部鹿打卡数据。
        """
        guarded = await self._guard(event)
        if not guarded:
            return
        group_id, user_id = guarded
        user_name = event.get_sender_name()

        try:
            async with aiosqlite.connect(self.deer_db_path) as conn:
                cursor = await conn.execute(
                    "DELETE FROM checkin WHERE user_id = ?", (user_id,)
                )
                await conn.commit()
                deleted_count = cursor.rowcount
            logger.info(
                f"用户 {user_name} ({user_id}) 清空了全部鹿打卡数据，共删除 {deleted_count} 条记录。"
            )
        except Exception as e:
            logger.error(f"清空用户 {user_name} ({user_id}) 的打卡数据失败: {e}")
            yield event.plain_result("清空失败，数据库出错了 >_<")
            return

        yield event.plain_result(
            f"已清空您的全部鹿打卡数据，共删除 {deleted_count} 条记录，重新开启新鹿生吧！"
        )

    @filter.regex(rf"^{KLITTRA_EMOJI_RE}清空$")
    async def handle_klittra_clear(self, event: AstrMessageEvent):
        """
        响应 '🤏清空' 命令，清空当前用户的全部扣日历数据。
        """
        # 检查是否启用了扣日历功能
        if not self.enable_female_calendar:
            return  # 未启用扣日历功能，不处理

        guarded = await self._guard(event)
        if not guarded:
            return
        group_id, user_id = guarded
        user_name = event.get_sender_name()

        try:
            async with aiosqlite.connect(self.klittra_db_path) as conn:
                cursor = await conn.execute(
                    "DELETE FROM klittra_checkin WHERE user_id = ?", (user_id,)
                )
                await conn.commit()
                deleted_count = cursor.rowcount
            logger.info(
                f"用户 {user_name} ({user_id}) 清空了全部扣日历数据，共删除 {deleted_count} 条记录。"
            )
        except Exception as e:
            logger.error(f"清空用户 {user_name} ({user_id}) 的扣日历数据失败: {e}")
            yield event.plain_result("清空失败，数据库出错了 >_<")
            return

        yield event.plain_result(
            f"已清空您的全部扣日历数据，共删除 {deleted_count} 条记录，重新开始吧！"
        )

    @filter.regex(r"^🦌群报$")
    async def handle_group_report(self, event: AstrMessageEvent):
        """
        响应 '🦌群报' 命令，生成并发送本群当日/当月的打卡战报图片。
        """
        guarded = await self._guard(event, require_group=True)
        if not guarded:
            if not event.get_group_id():
                yield event.plain_result("请在群聊中使用此功能！")
            return
        group_id, user_id = guarded
        current_time = datetime.now()
        today_str = self._get_adjusted_date(current_time)
        today_date = datetime.strptime(today_str, "%Y-%m-%d").date()
        # 上一个自然周：上周一 ~ 上周日
        this_monday = today_date - timedelta(days=today_date.weekday())
        week_start_str = (this_monday - timedelta(days=7)).strftime("%Y-%m-%d")
        week_end_str = (this_monday - timedelta(days=1)).strftime("%Y-%m-%d")
        current_month_str = today_str[:7]

        # 获取群成员列表，构造昵称映射（仅 aiocqhttp 平台）
        try:
            name_map = await self._build_member_name_map(event, group_id)
            if not name_map:
                logger.warning(f"无法获取群 {group_id} 的成员列表")
                yield event.plain_result("无法获取群成员信息，无法生成群报。")
                return
        except Exception as e:
            logger.error(f"获取群成员列表失败: {e}")
            yield event.plain_result("获取群成员信息时出错了 >_<")
            return

        group_user_ids = set(name_map.keys())

        def display_name(uid: str) -> str:
            return name_map.get(str(uid), str(uid))

        def find_unique_king(rows) -> tuple | None:
            """取唯一最高者作为鹿王；无人打卡或最高次数并列时返回 None。"""
            if not rows:
                return None
            max_count = max(r[1] for r in rows)
            kings = [r for r in rows if r[1] == max_count]
            if len(kings) != 1:
                return None
            return kings[0]

        today_rows = []
        week_rows = []
        month_rows = []
        try:
            # 范围下限取 min(上周一, 本月1号)，一次取回相关记录，
            # 今日/上周/本月三份数据在 Python 中分桶聚合，等价于原先 3 条查询
            range_start = min(week_start_str, today_str[:7] + "-01")
            async with aiosqlite.connect(self.deer_db_path) as conn:
                cursor = await conn.execute(
                    "SELECT checkin_date, user_id, deer_count, created_at FROM checkin WHERE checkin_date >= ? AND checkin_date <= ?",
                    (range_start, today_str),
                )
                rows = await cursor.fetchall()

            today_rows = [
                (uid, cnt, ts) for d, uid, cnt, ts in rows if d == today_str
            ]
            week_total = {}
            month_total = {}
            for d, uid, cnt, _ts in rows:
                if week_start_str <= d <= week_end_str:
                    week_total[uid] = week_total.get(uid, 0) + cnt
                if d[:7] == current_month_str:
                    month_total[uid] = month_total.get(uid, 0) + cnt
            week_rows = sorted(week_total.items(), key=lambda x: x[1], reverse=True)
            month_rows = sorted(month_total.items(), key=lambda x: x[1], reverse=True)
        except Exception as e:
            logger.error(f"查询群报数据失败: {e}")
            yield event.plain_result("查询群报数据时出错了 >_<")
            return

        # 过滤出本群成员
        today_group = [
            (uid, cnt, ts) for uid, cnt, ts in today_rows if str(uid) in group_user_ids
        ]
        week_group = [
            (uid, total) for uid, total in week_rows if str(uid) in group_user_ids
        ]
        month_group = [
            (uid, total) for uid, total in month_rows if str(uid) in group_user_ids
        ]

        # 与 🦌排行 一致：超过每月计入上限的用户不计入本月统计
        if self.monthly_max_checkins > 0:
            month_group = [
                (uid, total)
                for uid, total in month_group
                if total <= self.monthly_max_checkins
            ]

        # 今日首鹿（依赖 created_at）
        first_name = None
        first_time = None
        first_count = None
        first_undetermined = False
        today_with_time = [r for r in today_group if r[2] is not None]
        if today_with_time:
            today_with_time.sort(key=lambda x: x[2])
            fu, fc, fts = today_with_time[0]
            first_name = display_name(fu)
            first_time = fts[11:16] if len(fts) >= 16 else fts
            first_count = fc
        elif today_group:
            first_undetermined = True  # 今日已有人打卡但缺时间数据

        # 今日概况
        today_participants = len(today_group)
        today_total = sum(r[1] for r in today_group)

        # 上周鹿王（仅唯一最高者）
        week_top = None
        week_king = find_unique_king(week_group)
        if week_king:
            week_top = (display_name(week_king[0]), week_king[1])

        # 本月概况与前三
        month_participants = len(month_group)
        month_total = sum(total for _, total in month_group)
        month_top3 = [(display_name(uid), total) for uid, total in month_group[:3]]

        logger.info(
            f"群 {group_id} 群报数据: 今日 {today_participants} 人/{today_total} 次，本月 {month_participants} 人/{month_total} 次"
        )

        report_data = {
            "first_name": first_name,
            "first_time": first_time,
            "first_count": first_count,
            "first_undetermined": first_undetermined,
            "today_participants": today_participants,
            "today_total": today_total,
            "week_top": week_top,
            "month_participants": month_participants,
            "month_total": month_total,
            "month_top3": month_top3,
        }

        image_path = ""
        try:
            image_path = await asyncio.to_thread(
                self.deer_core._create_group_report_image,
                today_date.year,
                today_date.month,
                today_date.day,
                report_data,
            )
            yield event.image_result(image_path)
        except FileNotFoundError:
            logger.error("字体文件未找到！无法生成群报图片。")
            yield event.plain_result("服务器缺少字体文件，无法生成群报图片。")
        except Exception as e:
            logger.error(f"生成或发送群报图片失败: {e}")
            yield event.plain_result("处理群报图片时发生了未知错误 >_<")
        finally:
            if image_path and os.path.exists(image_path):
                try:
                    await asyncio.to_thread(os.remove, image_path)
                    logger.debug(f"已成功删除临时图片: {image_path}")
                except OSError as e:
                    logger.error(f"删除临时图片 {image_path} 失败: {e}")

    @filter.regex(r"^🦌生涯$")
    async def handle_deer_career(self, event: AstrMessageEvent):
        """
        响应 '🦌生涯' 命令，生成并发送用户的生涯统计报告。
        """
        guarded = await self._guard(event)
        if not guarded:
            return
        group_id, user_id = guarded
        user_name = event.get_sender_name()

        # 获取当前调整后的日期
        current_time = datetime.now()
        today_str = self._get_adjusted_date(current_time)
        today_date = datetime.strptime(today_str, "%Y-%m-%d").date()

        try:
            async with aiosqlite.connect(self.deer_db_path) as conn:
                # 一次性取出该用户全部打卡记录（行数≈打卡天数，量级极小），
                # 所有统计改为 Python 单遍计算，避免 5 次重复查询
                cursor = await conn.execute(
                    """
                    SELECT checkin_date, deer_count FROM checkin WHERE user_id = ?
                    ORDER BY checkin_date ASC
                """,
                    (user_id,),
                )
                rows = await cursor.fetchall()

            if not rows:
                yield event.plain_result(
                    "您还没有任何打卡记录，发送“🦌”开始您的生涯吧！"
                )
                return

            # 1. 基础数据
            first_date_str = rows[0][0]
            last_date_str = rows[-1][0]
            total_days = len(rows)
            total_count = sum(count for _, count in rows)

            first_date = datetime.strptime(first_date_str, "%Y-%m-%d").date()
            total_span_days = (today_date - first_date).days + 1  # 累计时光

            # 日均发射 (按总跨度天数)
            daily_avg = total_count / total_span_days if total_span_days else 0

            # 活跃占比 (动手天数 / 生涯总天数)
            active_ratio = (
                (total_days / total_span_days) * 100 if total_span_days else 0
            )

            # 2. 巅峰时刻 & 贤者时期
            # 按月份聚合（等价于 strftime('%Y-%m') GROUP BY）
            month_dict = {}
            for date_str, count in rows:
                month = date_str[:7]
                month_dict[month] = month_dict.get(month, 0) + count

            # 生成从 first_date 到 today_date 的所有月份
            all_months = []
            curr = first_date.replace(day=1)
            end_curr = today_date.replace(day=1)
            while curr <= end_curr:
                all_months.append(curr.strftime("%Y-%m"))
                # 下个月
                if curr.month == 12:
                    curr = curr.replace(year=curr.year + 1, month=1)
                else:
                    curr = curr.replace(month=curr.month + 1)

            # 填充 0 记录月份
            full_month_data = []
            for m in all_months:
                count = month_dict.get(m, 0)
                full_month_data.append((m, count))

            # 月度之最
            if full_month_data:
                full_month_data.sort(key=lambda x: x[1], reverse=True)
                max_month_str, max_month_count = full_month_data[0]

                # 最少月份排除当前月（当月未结束，统计不完整）
                min_candidates = [
                    m for m in full_month_data if m[0] != today_str[:7]
                ]
                if min_candidates:
                    min_candidates.sort(key=lambda x: x[1])  # 升序
                    min_month_str, min_month_count = min_candidates[
                        0
                    ]  # 最小的 (可能是0)
                else:
                    # 无其他月份可选（生涯首月即当前月），退化为使用当前月
                    min_month_str = today_str[:7]
                    min_month_count = month_dict.get(today_str[:7], 0)
            else:
                max_month_str, max_month_count = "N/A", 0
                min_month_str, min_month_count = "N/A", 0

            # 单日之最（并列取日期最新，与原 ORDER BY deer_count DESC, checkin_date DESC 一致）
            max_day_date, max_day_count = max(rows, key=lambda r: (r[1], r[0]))

            # 最长休养期 (连续未打卡)
            date_objs = [
                datetime.strptime(r[0], "%Y-%m-%d").date() for r in rows
            ]

            max_gap = 0
            gap_start = None
            gap_end = None

            # 检查所有间隔
            for i in range(len(date_objs) - 1):
                d1 = date_objs[i]
                d2 = date_objs[i + 1]
                gap = (d2 - d1).days - 1
                if gap > max_gap:
                    max_gap = gap
                    gap_start = d1 + timedelta(days=1)
                    gap_end = d2 - timedelta(days=1)

            # 检查最后一次打卡到今天的间隔 (如果今天没打卡)
            last_checkin_date = datetime.strptime(last_date_str, "%Y-%m-%d").date()
            days_since_last = (today_date - last_checkin_date).days

            if days_since_last > 0:
                # 进行中的休养若已超过历史记录则更新（按完整整天计，不含今天）
                if days_since_last - 1 > max_gap:
                    max_gap = days_since_last - 1
                    gap_start = last_checkin_date + timedelta(days=1)
                    gap_end = today_date - timedelta(days=1)  # 直到昨天

            rest_period_str = f"{max_gap} 天"
            if max_gap > 0 and gap_start and gap_end:
                rest_period_str += f" ({gap_start.strftime('%Y-%m-%d')} ~ {gap_end.strftime('%Y-%m-%d')})"
            elif max_gap == 0:
                rest_period_str = "0 天 (全勤特种兵)"

            # 生成贤者时刻评语
            sage_comment = ""
            if max_gap > 180:
                sage_comment = "可以去医院挂号了"
            elif max_gap >= 91:
                sage_comment = "戒色吧黄牌选手"
            elif max_gap >= 31:
                sage_comment = "设备已生锈，急需保养"
            elif max_gap >= 15:
                sage_comment = "已经开始戒色文学创作了是吧"
            elif max_gap >= 8:
                sage_comment = "没有那种世俗的欲望"
            elif max_gap >= 0:
                sage_comment = "你连一周都憋不住？"

            # 4. 当前状态
            status_day = days_since_last

            # 生成当前状态评语
            status_comment = ""
            if status_day == 0:
                status_comment = "别停，男人不能说不行"
            elif status_day == 1:
                status_comment = "年轻人要好好把握当下"
            elif 2 <= status_day <= 3:
                status_comment = "三天不练，手生；三天不鹿，心痒"
            elif 4 <= status_day <= 7:
                status_comment = "小鹿怡情啊兄弟"
            elif 8 <= status_day <= 14:
                status_comment = "你的国产欧美在等你宠幸"
            elif 15 <= status_day <= 21:
                status_comment = "半个月了，这还能忍？"
            elif 22 <= status_day <= 30:
                status_comment = "一个月没碰，你还是男人吗"
            elif status_day > 30:
                status_comment = "阳痿直说"

            # 5. 阶段性总结 (基于活跃占比和总次数)
            summary_comment = ""

            # 优先判断极端数据
            if total_count >= 2000:
                summary_comment = "陆地神仙"
            elif total_count >= 1000:
                if active_ratio > 60:
                    summary_comment = "鹿是我此生唯一的信仰"
                else:
                    summary_comment = "无他，唯手熟尔"

            # 高频玩家
            elif active_ratio > 80 and total_span_days > 30:
                summary_comment = "一天不鹿，浑身难受"
            elif active_ratio > 50 and total_span_days > 30:
                summary_comment = "两天不鹿，留之何用"

            # 资深玩家
            elif total_count >= 500:
                summary_comment = "阅片无数，心中无码"
            elif total_count >= 200:
                if max_gap > 30:
                    summary_comment = "自律使人成功"
                else:
                    summary_comment = "劳模典范"

            # 特殊/佛系
            elif total_span_days > 365 and total_count < 20:
                summary_comment = "我的剑不轻易出鞘"
            elif active_ratio < 5 and total_span_days > 90:
                summary_comment = "戒色！"

            # 萌新
            elif total_count < 10:
                summary_comment = "少年始知鹿滋味"
            elif total_count < 50:
                summary_comment = "任重而道远"

            # 兜底
            else:
                summary_comment = "同志还需努力"

            # 7. 本月进度 vs 上月同期（当前状态用）
            current_month_str = today_str[:7]
            cur_year, cur_month = map(int, current_month_str.split("-"))
            if cur_month == 1:
                last_month_str = f"{cur_year - 1}-12"
            else:
                last_month_str = f"{cur_year}-{cur_month - 1:02d}"
            this_month_count = 0
            last_month_same_period = 0
            today_day = today_date.day
            for date_str, count in rows:
                date_prefix = date_str[:7]
                day = int(date_str.split("-")[2])
                if date_prefix == current_month_str:
                    this_month_count += count
                elif date_prefix == last_month_str and day <= today_day:
                    last_month_same_period += count

            if first_date_str[:7] == current_month_str:
                # 生涯首月即当前月，无上月同期可比
                month_progress = f"本月进度：{this_month_count} 次（本月启程）"
            else:
                diff = this_month_count - last_month_same_period
                if diff > 0:
                    month_progress = (
                        f"本月进度：{this_month_count} 次（较上月同期 +{diff}）"
                    )
                elif diff < 0:
                    month_progress = (
                        f"本月进度：{this_month_count} 次（较上月同期 {diff}）"
                    )
                else:
                    month_progress = (
                        f"本月进度：{this_month_count} 次（与上月同期持平）"
                    )

        except Exception as e:
            logger.error(f"生成生涯报告数据失败: {e}")
            yield event.plain_result("生成生涯报告时出错了 >_<")
            return

        # 准备数据给绘图函数
        stats = {
            "first_date_str": first_date_str,
            "total_span_days": total_span_days,
            "total_count": total_count,
            "total_days": total_days,
            "daily_avg": daily_avg,
            "active_ratio": active_ratio,
            "max_day_date": max_day_date,
            "max_day_count": max_day_count,
            "max_month_str": max_month_str,
            "max_month_count": max_month_count,
            "min_month_str": min_month_str,
            "min_month_count": min_month_count,
            "rest_period_str": rest_period_str,
            "sage_comment": sage_comment,
            "status_day": status_day,
            "status_comment": status_comment,
            "month_progress": month_progress,
            "summary_comment": summary_comment,
        }

        # 生成图片
        image_path = ""
        try:
            image_path = await asyncio.to_thread(
                self.deer_core._create_career_image, user_name, stats
            )
            # 发送图片
            yield event.image_result(image_path)
        except Exception as e:
            logger.error(f"生成生涯图片失败: {e}")
            yield event.plain_result("生成生涯图片失败 >_<")
        finally:
            # 删除临时图片文件
            if image_path and os.path.exists(image_path):
                try:
                    await asyncio.to_thread(os.remove, image_path)
                    logger.debug(f"已成功删除临时图片: {image_path}")
                except OSError as e:
                    logger.error(f"删除临时图片 {image_path} 失败: {e}")

    @filter.regex(r"^🦌排行(?:\s+(\d{1,2}|\d{4}))?$")
    async def handle_deer_ranking(self, event: AstrMessageEvent):
        """
        响应 '🦌排行' 命令，生成并发送打卡排行榜图片。
        不带参数：本月；🦌排行 11：11月；🦌排行 2025：2025年全年。
        """
        guarded = await self._guard(event, require_group=True)
        if not guarded:
            if not event.get_group_id():
                yield event.plain_result("请在群聊中使用此功能！")
            return
        group_id, user_id = guarded

        pattern = r"^🦌排行(?:\s+(\d{1,2}|\d{4}))?$"
        match = re.search(pattern, event.message_str)
        param = match.group(1) if match and match.group(1) else None

        if param is None:
            current_time = datetime.now()
            adjusted_date_str = self._get_adjusted_date(current_time)
            target_year = int(adjusted_date_str[:4])
            target_month = int(adjusted_date_str[5:7])
            is_month = True
            period_label = f"{target_year}年{target_month}月"
        else:
            kind, target_year, target_month, err = self._parse_period_param(param)
            if kind is None:
                yield event.plain_result(err)
                return
            is_month = kind == "month"
            period_label = (
                f"{target_year}年{target_month}月" if is_month else f"{target_year}年"
            )

        logger.info(f"开始查询群 {group_id} 的 {period_label} 排行榜数据")

        # 查询该周期所有用户的打卡数据
        all_users_data = []
        try:
            async with aiosqlite.connect(self.deer_db_path) as conn:
                # 范围查询替代 strftime，可直接利用主键索引前缀
                if is_month:
                    period_start = f"{target_year}-{target_month:02d}-01"
                    next_year, next_month = (
                        (target_year + 1, 1)
                        if target_month == 12
                        else (target_year, target_month + 1)
                    )
                    period_end = f"{next_year}-{next_month:02d}-01"
                else:
                    period_start = f"{target_year}-01-01"
                    period_end = f"{target_year + 1}-01-01"
                sql = "SELECT user_id, SUM(deer_count) as total_deer FROM checkin WHERE checkin_date >= ? AND checkin_date < ? GROUP BY user_id ORDER BY total_deer DESC"
                async with conn.execute(sql, (period_start, period_end)) as cursor:
                    rows = await cursor.fetchall()
                    for row in rows:
                        user_id, total_deer = row
                        all_users_data.append((user_id, total_deer))
            logger.info(f"查询到 {len(all_users_data)} 个用户的打卡数据")
        except Exception as e:
            logger.error(f"查询 {period_label} 排行榜数据失败: {e}")
            yield event.plain_result("查询排行榜数据时出错了 >_<")
            return

        if not all_users_data:
            logger.info(f"{period_label} 没有任何打卡记录")
            yield event.plain_result(
                f"{self._period_name(is_month, target_year, target_month)}还没有任何打卡记录哦，快发送“🦌”开始打卡吧！"
            )
            return

        # 获取当前群的所有成员
        try:
            name_map = await self._build_member_name_map(event, group_id)
            if not name_map:
                logger.warning(f"无法获取群 {group_id} 的成员列表")
                yield event.plain_result("无法获取群成员信息，无法生成排行榜。")
                return
        except Exception as e:
            logger.error(f"获取群成员列表失败: {e}")
            yield event.plain_result("获取群成员信息时出错了 >_<")
            return

        group_user_ids = set(name_map.keys())

        # 过滤出当前群的用户
        ranking_data = [
            (user_id, deer_count)
            for user_id, deer_count in all_users_data
            if str(user_id) in group_user_ids
        ]

        # 每月上限仅对月度排行生效；年度排行按年汇总，不适用月度上限
        if is_month and self.monthly_max_checkins > 0:
            ranking_data = [
                (user_id, deer_count)
                for user_id, deer_count in ranking_data
                if deer_count <= self.monthly_max_checkins
            ]

        # 只取前self.ranking_display_count名（默认10名）
        ranking_display_count = getattr(
            self, "ranking_display_count", 10
        )  # 默认显示10名
        total_participants = len(ranking_data)
        ranking_data = ranking_data[:ranking_display_count]

        if not ranking_data:
            logger.info(
                f"群 {group_id} 中 {period_label} 没有用户有打卡记录，所有 {len(all_users_data)} 个有记录的用户都不在群中或超过限制"
            )
            yield event.plain_result(
                f"{self._period_name(is_month, target_year, target_month)}本群还没有任何打卡记录哦，快发送“🦌”开始打卡吧！"
            )
            return

        # 从群成员列表构造昵称
        user_names = [name_map.get(str(uid), f"用户{uid}") for uid, _ in ranking_data]

        # 生成排行榜图片
        image_path = ""
        try:
            image_path = await asyncio.to_thread(
                self._create_ranking_image,
                user_names,
                ranking_data,
                target_year,
                target_month,
                total_participants,
                period_label,
            )
            yield event.image_result(image_path)
        except FileNotFoundError:
            logger.error("字体文件未找到！无法生成排行榜图片。")
            ranking_text = f"🦌{period_label}打卡排行榜:\n"
            for i, (user_name, deer_count) in enumerate(
                zip(user_names, [data[1] for data in ranking_data]), 1
            ):
                ranking_text += f"{i}. {user_name}: {deer_count}次\n"
            yield event.plain_result(ranking_text)
        except Exception as e:
            logger.error(f"生成或发送排行榜图片失败: {e}")
            yield event.plain_result("处理排行榜图片时发生了未知错误 >_<")
        finally:
            if image_path and os.path.exists(image_path):
                try:
                    await asyncio.to_thread(os.remove, image_path)
                    logger.debug(f"已成功删除临时图片: {image_path}")
                except OSError as e:
                    logger.error(f"删除临时图片 {image_path} 失败: {e}")

    @filter.regex(r"^🦌(?:分析|报告)(?:\s+(\d{1,2}|\d{4}))?$")
    async def handle_analysis(self, event: AstrMessageEvent):
        """
        响应 '🦌分析' 命令，生成并发送打卡分析报告。
        不带参数：分析本月数据
        一到两位数字：分析指定月份数据
        四位数字：分析指定年份数据
        """
        guarded = await self._guard(event)
        if not guarded:
            return
        group_id, user_id = guarded
        pattern = r"^🦌(?:分析|报告)(?:\s+(\d{1,2}|\d{4}))?$"
        match = re.search(pattern, event.message_str)

        user_name = event.get_sender_name()

        # 解析参数
        param = match.group(1) if match and match.group(1) else None

        if param is None:
            # 默认分析本月
            current_date = datetime.now()
            target_year = current_date.year
            target_month = current_date.month
            target_period = f"{target_year}年{target_month}月"

            # 查询本月数据
            period_data = await self._get_user_period_data(
                user_id, target_year, target_month
            )

            # 生成分析报告
            (
                analysis_result,
                checkin_rate,
            ) = await self._generate_monthly_analysis_report(
                user_name, target_year, target_month, period_data
            )
        else:
            kind, target_year, target_month, err = self._parse_period_param(param)
            if kind is None:
                yield event.plain_result(err)
                return
            if kind == "month":
                target_period = f"{target_year}年{target_month}月"

                # 查询指定月份数据
                period_data = await self._get_user_period_data(
                    user_id, target_year, target_month
                )

                # 生成分析报告
                (
                    analysis_result,
                    checkin_rate,
                ) = await self._generate_monthly_analysis_report(
                    user_name, target_year, target_month, period_data
                )
            else:  # 'year'
                target_period = f"{target_year}年"

                # 查询指定年份数据
                yearly_data = await self._get_user_yearly_data(user_id, target_year)

                # 生成年份分析报告
                analysis_result = await self._generate_yearly_analysis_report(
                    user_name, target_year, yearly_data
                )

        logger.info(
            f"用户 {user_name} ({user_id}) 请求查看 {target_period} 的分析报告。"
        )

        if not analysis_result:
            yield event.plain_result(
                f"您在{target_period}还没有打卡记录哦，发送“🦌”开始打卡吧！"
            )
            return

        # 生成并发送分析图片
        image_path = ""
        try:
            image_path = await asyncio.to_thread(
                self._create_analysis_image,
                user_name,
                target_period,
                analysis_result,
                checkin_rate if "checkin_rate" in locals() else 0.0,
            )
            yield event.image_result(image_path)
        except FileNotFoundError:
            logger.error("字体文件未找到！无法生成分析图片。")
            yield event.plain_result(analysis_result)
        except Exception as e:
            logger.error(f"生成或发送分析图片失败: {e}")
            yield event.plain_result("处理分析图片时发生了未知错误 >_<")
        finally:
            if image_path and os.path.exists(image_path):
                try:
                    await asyncio.to_thread(os.remove, image_path)
                    logger.debug(f"已成功删除临时图片: {image_path}")
                except OSError as e:
                    logger.error(f"删除临时图片 {image_path} 失败: {e}")

    @filter.regex(r"^🦌年历(?:\s+(\d{4}))?$")
    async def handle_yearly_calendar(self, event: AstrMessageEvent):
        """
        响应 '🦌年历' 命令，生成并发送指定年份的完整打卡日历图片。
        不带参数默认当年。
        """
        guarded = await self._guard(event)
        if not guarded:
            return
        group_id, user_id = guarded

        # 解析年份参数
        pattern = r"^🦌年历(?:\s+(\d{4}))?$"
        match = re.search(pattern, event.message_str)

        current_year = datetime.now().year
        if match and match.group(1):
            try:
                current_year = int(match.group(1))
            except ValueError:
                pass  # 如果解析失败，回退到当前年份

        user_name = event.get_sender_name()

        logger.info(f"用户 {user_name} ({user_id}) 请求查看 {current_year}年的年历。")

        # 查询今年所有月份的打卡记录
        yearly_data = {}
        try:
            async with aiosqlite.connect(self.deer_db_path) as conn:
                async with conn.execute(
                    "SELECT checkin_date, deer_count FROM checkin WHERE user_id = ? AND strftime('%Y', checkin_date) = ?",
                    (user_id, str(current_year)),
                ) as cursor:
                    rows = await cursor.fetchall()
                    if not rows:
                        yield event.plain_result(
                            f"您在{current_year}年还没有打卡记录哦，发送“🦌”开始打卡吧！"
                        )
                        return

                    for row in rows:
                        date_str = row[0]
                        count = row[1]
                        year, month, day = date_str.split("-")
                        month = int(month)
                        day = int(day)

                        if month not in yearly_data:
                            yearly_data[month] = {}
                        yearly_data[month][day] = count
        except Exception as e:
            logger.error(
                f"查询用户 {user_name} ({user_id}) 的 {current_year}年数据失败: {e}"
            )
            yield event.plain_result("查询年历数据时出错了 >_<")
            return

        # 生成并发送年历图片
        image_path = ""
        try:
            image_path = await asyncio.to_thread(
                self._create_yearly_calendar_image,
                user_id,
                user_name,
                current_year,
                yearly_data,
            )
            yield event.image_result(image_path)
        except FileNotFoundError:
            logger.error("字体文件未找到！无法生成年历图片。")
            # 生成文本总结
            total_months = len(yearly_data)
            total_days = sum(len(days) for days in yearly_data.values())
            total_deer = sum(sum(days.values()) for days in yearly_data.values())
            yield event.plain_result(
                f"服务器缺少字体文件，无法生成年历图片。{current_year}年您已打卡{total_months}个月，{total_days}天，累计{total_deer}个🦌。"
            )
        except Exception as e:
            logger.error(f"生成或发送年历图片失败: {e}")
            yield event.plain_result("处理年历图片时发生了未知错误 >_<")
        finally:
            if image_path and os.path.exists(image_path):
                try:
                    await asyncio.to_thread(os.remove, image_path)
                    logger.debug(f"已成功删除临时图片: {image_path}")
                except OSError as e:
                    logger.error(f"删除临时图片 {image_path} 失败: {e}")

    @filter.regex(rf"^{KLITTRA_EMOJI_RE}年历(?:\s+(\d{{4}}))?$")
    async def handle_klittra_yearly_calendar(self, event: AstrMessageEvent):
        """
        响应 '🤏年历' 命令，生成并发送指定年份的完整扣日历图片。
        不带参数默认当年。
        """
        # 检查是否启用了扣日历功能
        if not self.enable_female_calendar:
            return  # 未启用扣日历功能，不处理

        guarded = await self._guard(event)
        if not guarded:
            return
        group_id, user_id = guarded

        # 解析年份参数
        pattern = rf"^{KLITTRA_EMOJI_RE}年历(?:\s+(\d{{4}}))?$"
        match = re.search(pattern, event.message_str)

        current_year = datetime.now().year
        if match and match.group(1):
            try:
                current_year = int(match.group(1))
            except ValueError:
                pass  # 如果解析失败，回退到当前年份

        user_name = event.get_sender_name()

        logger.info(f"用户 {user_name} ({user_id}) 请求查看 {current_year}年的扣年历。")

        # 查询今年所有月份的扣日历记录
        yearly_data = {}
        try:
            async with aiosqlite.connect(self.klittra_db_path) as conn:
                async with conn.execute(
                    "SELECT checkin_date, klittra_count FROM klittra_checkin WHERE user_id = ? AND strftime('%Y', checkin_date) = ?",
                    (user_id, str(current_year)),
                ) as cursor:
                    rows = await cursor.fetchall()
                    if not rows:
                        yield event.plain_result(
                            f"您在{current_year}年还没有扣日历记录哦，发送“🤏”开始记录吧！"
                        )
                        return

                    for row in rows:
                        date_str = row[0]
                        count = row[1]
                        year, month, day = date_str.split("-")
                        month = int(month)
                        day = int(day)

                        if month not in yearly_data:
                            yearly_data[month] = {}
                        yearly_data[month][day] = count
        except Exception as e:
            logger.error(
                f"查询用户 {user_name} ({user_id}) 的 {current_year}年扣日历数据失败: {e}"
            )
            yield event.plain_result("查询扣日历数据时出错了 >_<")
            return

        # 生成并发送扣年历图片
        image_path = ""
        try:
            image_path = await asyncio.to_thread(
                self.klittra_core._create_klittra_yearly_calendar_image,
                user_id,
                user_name,
                current_year,
                yearly_data,
            )
            yield event.image_result(image_path)
        except FileNotFoundError:
            logger.error("字体文件未找到！无法生成扣日历图片。")
            # 生成文本总结
            total_months = len(yearly_data)
            total_days = sum(len(days) for days in yearly_data.values())
            total_deer = sum(sum(days.values()) for days in yearly_data.values())
            yield event.plain_result(
                f"服务器缺少字体文件，无法生成扣日历图片。{current_year}年您已扣了{total_months}个月，{total_days}天，共{total_deer}次。"
            )
        except Exception as e:
            logger.error(f"生成或发送扣日历图片失败: {e}")
            yield event.plain_result("处理扣日历图片时发生了未知错误 >_<")
        finally:
            if image_path and os.path.exists(image_path):
                try:
                    await asyncio.to_thread(os.remove, image_path)
                    logger.debug(f"已成功删除临时图片: {image_path}")
                except OSError as e:
                    logger.error(f"删除临时图片 {image_path} 失败: {e}")

    @filter.regex(rf"^{KLITTRA_EMOJI_RE}月历\s+(\d{{1,2}})$")
    async def handle_klittra_specific_month_calendar(self, event: AstrMessageEvent):
        """
        响应 '🤏月历 X' 命令，生成并发送指定月份的扣日历图片。
        """
        # 检查是否启用了扣日历功能
        if not self.enable_female_calendar:
            return  # 未启用扣日历功能，不处理

        guarded = await self._guard(event)
        if not guarded:
            return
        group_id, user_id = guarded

        pattern = rf"^{KLITTRA_EMOJI_RE}月历\s+(\d{{1,2}})$"
        match = re.search(pattern, event.message_str)
        if not match:
            yield event.plain_result(
                "命令格式错误，请使用：🤏月历 月份（如：🤏月历 11）"
            )
            return

        try:
            target_month = int(match.group(1))
            if not (1 <= target_month <= 12):
                yield event.plain_result("月份必须在1-12之间哦！")
                return
        except ValueError:
            yield event.plain_result("请输入正确的月份数字！")
            return

        # 计算年份：如果指定月份大于当前月份，则为去年
        current_date = datetime.now()
        current_month = current_date.month
        current_year = current_date.year

        if target_month > current_month:
            target_year = current_year - 1
        else:
            target_year = current_year

        target_month_str = f"{target_year}-{target_month:02d}"
        user_name = event.get_sender_name()

        logger.info(
            f"用户 {user_name} ({user_id}) 请求查看 {target_year}年{target_month}月的扣日历。"
        )

        # 查询指定月份的扣日历记录
        checkin_records = {}
        total_deer_this_month = 0
        try:
            async with aiosqlite.connect(self.klittra_db_path) as conn:
                async with conn.execute(
                    "SELECT checkin_date, klittra_count FROM klittra_checkin WHERE user_id = ? AND strftime('%Y-%m', checkin_date) = ?",
                    (user_id, target_month_str),
                ) as cursor:
                    rows = await cursor.fetchall()
                    if not rows:
                        yield event.plain_result(
                            f"您在{target_year}年{target_month}月还没有扣日历记录哦，发送“🤏”开始记录吧！"
                        )
                        return

                    for row in rows:
                        day = int(row[0].split("-")[2])
                        count = row[1]
                        checkin_records[day] = count
                        total_deer_this_month += count
        except Exception as e:
            logger.error(
                f"查询用户 {user_name} ({user_id}) 的 {target_year}年{target_month}月扣日历数据失败: {e}"
            )
            yield event.plain_result("查询扣日历数据时出错了 >_<")
            return

        # 生成并发送扣日历图片
        image_path = ""
        try:
            image_path = await asyncio.to_thread(
                self.klittra_core._create_klittra_calendar_image,
                user_id,
                user_name,
                target_year,
                target_month,
                checkin_records,
                total_deer_this_month,
            )
            yield event.image_result(image_path)
        except FileNotFoundError:
            logger.error("字体文件未找到！无法生成扣日历图片。")
            yield event.plain_result(
                f"服务器缺少字体文件，无法生成扣日历图片。{target_year}年{target_month}月您已扣了{len(checkin_records)}天，共{total_deer_this_month}次。"
            )
        except Exception as e:
            logger.error(f"生成或发送扣日历图片失败: {e}")
            yield event.plain_result("处理扣日历图片时发生了未知错误 >_<")
        finally:
            if image_path and os.path.exists(image_path):
                try:
                    await asyncio.to_thread(os.remove, image_path)
                    logger.debug(f"已成功删除临时图片: {image_path}")
                except OSError as e:
                    logger.error(f"删除临时图片 {image_path} 失败: {e}")

    @filter.regex(rf"^{KLITTRA_EMOJI_RE}排行$")
    async def handle_klittra_ranking(self, event: AstrMessageEvent):
        """
        响应 '扣日历排行' 命令，生成并发送当前月度的扣日历排行榜图片。
        """
        guarded = await self._guard(event, require_group=True)
        if not guarded:
            if not event.get_group_id():
                yield event.plain_result("请在群聊中使用此功能！")
            return
        group_id, user_id = guarded
        current_time = datetime.now()
        adjusted_date_str = self._get_adjusted_date(current_time)
        current_year = int(adjusted_date_str[:4])
        current_month = int(adjusted_date_str[5:7])
        current_month_str = adjusted_date_str[:7]

        logger.info(f"开始查询群 {group_id} 的 {current_month_str} 月扣日历排行榜数据")

        # 查询当月所有用户的扣日历数据
        all_users_data = []
        try:
            async with aiosqlite.connect(self.klittra_db_path) as conn:
                async with conn.execute(
                    "SELECT user_id, SUM(klittra_count) as total_klittra FROM klittra_checkin WHERE strftime('%Y-%m', checkin_date) = ? GROUP BY user_id ORDER BY total_klittra DESC",
                    (current_month_str,),
                ) as cursor:
                    rows = await cursor.fetchall()
                    for row in rows:
                        user_id, total_klittra = row
                        all_users_data.append((user_id, total_klittra))
            logger.info(f"查询到 {len(all_users_data)} 个用户的扣日历数据")
        except Exception as e:
            logger.error(f"查询当月扣日历排行榜数据失败: {e}")
            yield event.plain_result("查询扣日历排行榜数据时出错了 >_<")
            return

        if not all_users_data:
            logger.info("本月没有任何扣日历记录")
            yield event.plain_result(
                "本月还没有任何扣日历记录哦，快发送“🤏”开始扣日历吧！"
            )
            return

        # 获取当前群的所有成员
        try:
            name_map = await self._build_member_name_map(event, group_id)
            if not name_map:
                logger.warning(f"无法获取群 {group_id} 的成员列表")
                yield event.plain_result("无法获取群成员信息，无法生成扣日历排行榜。")
                return
        except Exception as e:
            logger.error(f"获取群成员列表失败: {e}")
            yield event.plain_result("获取群成员信息时出错了 >_<")
            return

        group_user_ids = set(name_map.keys())

        # 过滤出当前群的用户
        ranking_data = [
            (user_id, klittra_count)
            for user_id, klittra_count in all_users_data
            if str(user_id) in group_user_ids
        ]

        # 根据配置的每月上限过滤数据（如果设置了限制）
        if self.monthly_max_checkins > 0:
            ranking_data = [
                (user_id, klittra_count)
                for user_id, klittra_count in ranking_data
                if klittra_count <= self.monthly_max_checkins
            ]

        # 只取前self.ranking_display_count名（默认10名）
        ranking_display_count = getattr(
            self, "ranking_display_count", 10
        )  # 默认显示10名
        total_participants = len(ranking_data)
        ranking_data = ranking_data[:ranking_display_count]

        if not ranking_data:
            logger.info(
                f"群 {group_id} 中本月没有用户有扣日历记录，所有 {len(all_users_data)} 个有记录的用户都不在群中或超过限制"
            )
            yield event.plain_result(
                "本月本群还没有任何扣日历记录哦，快发送“🤏”开始扣日历吧！"
            )
            return

        # 从群成员列表构造昵称
        user_names = [name_map.get(str(uid), f"用户{uid}") for uid, _ in ranking_data]

        # 生成排行榜图片
        image_path = ""
        try:
            image_path = await asyncio.to_thread(
                self.klittra_core._create_klittra_ranking_image,
                user_names,
                ranking_data,
                current_year,
                current_month,
                total_participants,
            )
            yield event.image_result(image_path)
        except FileNotFoundError:
            logger.error("字体文件未找到！无法生成扣日历排行榜图片。")
            ranking_text = f"🤏{current_year}年{current_month}月扣日历排行榜:\n"
            for i, (user_name, klittra_count) in enumerate(
                zip(user_names, [data[1] for data in ranking_data]), 1
            ):
                ranking_text += f"{i}. {user_name}: {klittra_count}次\n"
            yield event.plain_result(ranking_text)
        except Exception as e:
            logger.error(f"生成或发送扣日历排行榜图片失败: {e}")
            yield event.plain_result("处理扣日历排行榜图片时发生了未知错误 >_<")
        finally:
            if image_path and os.path.exists(image_path):
                try:
                    await asyncio.to_thread(os.remove, image_path)
                    logger.debug(f"已成功删除临时图片: {image_path}")
                except OSError as e:
                    logger.error(f"删除临时图片 {image_path} 失败: {e}")

    @filter.regex(r"^🦌帮助$")
    async def handle_help_command(self, event: AstrMessageEvent):
        """
        响应 '🦌帮助' 命令，发送一个包含所有指令用法的菜单。
        """
        guarded = await self._guard(event)
        if not guarded:
            return
        group_id, user_id = guarded
        help_text = (
            "--- 🦌打卡帮助菜单 ---\n\n"
            "1️⃣  🦌打卡\n"
            "    ▸ 命令: 直接发送 🦌 (可发送多个)\n"
            "    ▸ 作用: 记录今天🦌的数量。\n"
            "    ▸ 示例: 🦌🦌🦌\n\n"
            "2️⃣  查看日历\n"
            "    ▸ 命令: 🦌日历 [月份]，🦌年历 [年份]\n"
            "    ▸ 作用: 查看打卡日历，不记录打卡。不带参数默认本月或本年。\n"
            "    ▸ 示例: 🦌日历、🦌日历 11、🦌年历 2025\n\n"
            "3️⃣  查看生涯分析\n"
            "    ▸ 命令: 🦌生涯\n"
            "    ▸ 作用: 查看您的生涯统计、称号、最长记录等。\n\n"
            "4️⃣  打卡分析\n"
            "    ▸ 命令: 🦌报告 [月份/年份]\n"
            "    ▸ 作用: 分析您的打卡数据并生成报告。\n"
            "    ▸ 示例: 🦌报告 (本月分析)、🦌报告 11 (11月分析)、🦌报告 2025 (2025年分析)\n\n"
            "5️⃣  排行榜\n"
            "    ▸ 命令: 🦌排行 [月份/年份]\n"
            "    ▸ 作用: 查看本群打卡排行榜。不带参数默认本月。\n"
            "    ▸ 示例: 🦌排行、🦌排行 11、🦌排行 2025\n\n"
            "6️⃣  本群战报\n"
            "    ▸ 命令: 🦌群报\n"
            "    ▸ 作用: 生成本群今日首鹿、今日概况、上周鹿王、本月概况与本月前三的打卡战报图片。\n\n"
            "7️⃣  补签\n"
            "    ▸ 命令: 🦌补签 [日期] [次数]\n"
            "    ▸ 作用: 为本月指定日期补上打卡记录。\n"
            "    ▸ 示例: 🦌补签 5 1 (为本月5号补签1次)，🦌补签 5 (为本月5号补签1次)\n\n"
            "8️⃣  撤销\n"
            "    ▸ 命令: 🦌撤销 [日期] [次数]\n"
            "    ▸ 作用: 为本月指定日期减少打卡记录。\n"
            "    ▸ 示例: 🦌撤销 5 1 (为本月5号减少1次)，🦌撤销 5 (为本月5号减少1次)\n\n"
            "9️⃣  清空数据\n"
            "    ▸ 命令: 🦌清空\n"
            "    ▸ 作用: 清空您的全部鹿打卡数据，所有记录将被删除，无法恢复。\n\n"
            "🔟  显示此帮助\n"
            "    ▸ 命令: 🦌帮助\n"
        )
        if self.enable_female_calendar:
            help_text += "\n📌 已开启扣日历（🤏）功能：发送 🤏 打卡，🤏日历 查看，🤏清空 清空数据。\n"
        help_text += "\n祝您一🦌顺畅！"
        yield event.plain_result(help_text)

    async def _build_member_name_map(
        self, event: AstrMessageEvent, group_id: str
    ) -> dict:
        """获取群成员列表并构造 user_id -> 昵称 映射"""
        return await self.deer_core._build_member_name_map(event, group_id)

    def _create_ranking_image(
        self,
        user_names: list,
        ranking_data: list,
        year: int,
        month: int,
        total_participants: int = None,
        period_label: str = None,
    ) -> str:
        """
        绘制月度打卡排行榜图片，参考日历图片风格
        """
        return self.deer_core._create_ranking_image(
            user_names, ranking_data, year, month, total_participants, period_label
        )

    async def _get_user_period_data(self, user_id: str, year: int, month: int) -> dict:
        """获取用户指定月份的打卡数据"""
        return await self.deer_core._get_user_period_data(user_id, year, month)

    async def _get_user_yearly_data(self, user_id: str, year: int) -> dict:
        """获取用户指定年份的打卡数据"""
        return await self.deer_core._get_user_yearly_data(user_id, year)

    async def _generate_monthly_analysis_report(
        self, user_name: str, year: int, month: int, period_data: dict
    ) -> tuple[str, float]:
        """生成月度趣味打卡分析报告"""
        return await self.deer_core._generate_monthly_analysis_report(
            user_name, year, month, period_data
        )

    async def _generate_yearly_analysis_report(
        self, user_name: str, year: int, yearly_data: dict
    ) -> str:
        """生成年度趣味打卡分析报告（无emoji版）"""
        return await self.deer_core._generate_yearly_analysis_report(
            user_name, year, yearly_data
        )

    def _create_analysis_image(
        self,
        user_name: str,
        target_period: str,
        analysis_result: str,
        checkin_rate: float = 0.0,
    ) -> str:
        """
        绘制分析报告图片
        """
        return self.deer_core._create_analysis_image(
            user_name, target_period, analysis_result, checkin_rate
        )

    def _wrap_text(self, text: str, font, max_width: int) -> list:
        """
        文本自动换行
        """
        return self.deer_core._wrap_text(text, font, max_width)

    def _create_yearly_calendar_image(
        self, user_id: str, user_name: str, year: int, yearly_data: dict
    ) -> str:
        """
        绘制年度打卡日历图片，将12个月的日历按网格排列
        """
        return self.deer_core._create_yearly_calendar_image(
            user_id, user_name, year, yearly_data
        )

    def _create_calendar_image(
        self,
        user_id: str,
        user_name: str,
        year: int,
        month: int,
        checkin_data: dict,
        total_deer: int,
    ) -> str:
        """
        绘制用户月度打卡日历图片
        """
        return self.deer_core._create_calendar_image(
            user_id, user_name, year, month, checkin_data, total_deer
        )

    async def _generate_and_send_calendar(
        self, event: AstrMessageEvent, adjusted_date_str: str = None
    ):
        """查询和生成当月的打卡日历。"""
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()

        # 使用 deer_core 方法
        (
            result_text,
            image_path,
            has_error,
        ) = await self.deer_core._generate_and_send_calendar(
            event, user_id, user_name, self.deer_db_path, adjusted_date_str
        )

        if result_text:
            yield event.plain_result(result_text)
            if has_error:
                return

        if image_path:
            yield event.image_result(image_path)
        else:
            # 如果没有图片路径且没有错误，表示没有数据
            if not result_text:  # 仅当没有提供自定义结果时显示默认消息
                yield event.plain_result(
                    "您本月还没有打卡记录哦，发送“🦌”开始第一次打卡吧！"
                )

        # 删除临时图片文件
        if image_path and os.path.exists(image_path):
            try:
                await asyncio.to_thread(os.remove, image_path)
                logger.debug(f"已成功删除临时图片: {image_path}")
            except OSError as e:
                logger.error(f"删除临时图片 {image_path} 失败: {e}")

    async def terminate(self):
        """插件卸载/停用时调用"""
        logger.info("鹿打卡插件已卸载。")
