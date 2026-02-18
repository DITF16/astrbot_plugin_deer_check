import aiosqlite
import calendar
from datetime import date, datetime, timedelta
import os
import re
import time
import asyncio
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.star import StarTools
from .resources.deer_core import DeerCore
from .resources.klittra_core import KlittraCore

FONT_FILE = "font.ttf"
DEER_DB_NAME = "deer_checkin.db"
KLITTRA_DB_NAME = "klittra_checkin.db"


@register(
    "astrbot_plugin_deer_check",
    "DITF16&Foolllll",
    "一个发送'🦌'表情进行打卡并生成月度日历的插件",
    "1.3"
)
class DeerCheckinPlugin(Star):
    def __init__(self, context: Context, config: dict = None):
        super().__init__(context)
        self.config = config if config is not None else {}

        # 配置项
        self.group_whitelist = self.config.get("group_whitelist", [])
        self.user_blacklist = self.config.get("user_blacklist", [])
        self.day_start_time = self.config.get("day_start_time", "00:00")
        self.auto_delete_last_month_data = bool(self.config.get("auto_delete_last_month_data", True))
        self.daily_max_checkins = int(self.config.get("daily_max_checkins", 0))
        self.monthly_max_checkins = int(self.config.get("monthly_max_checkins", 0))
        self.enable_female_calendar = bool(self.config.get("enable_female_calendar", False))
        self.ranking_display_count = int(self.config.get("ranking_display_count", 10))

        data_dir = StarTools.get_data_dir("astrbot_plugin_deer_check")
        os.makedirs(data_dir, exist_ok=True)
        plugin_dir = os.path.dirname(__file__)
        resources_dir = os.path.join(plugin_dir, "resources")
        self.deer_db_path = os.path.join(data_dir, DEER_DB_NAME)
        self.klittra_db_path = os.path.join(data_dir, KLITTRA_DB_NAME)
        self.font_path = os.path.join(resources_dir, FONT_FILE)
        self.temp_dir = os.path.join(plugin_dir, "tmp")
        os.makedirs(self.temp_dir, exist_ok=True)

        self.deer_core = DeerCore(self.font_path, self.deer_db_path, self.temp_dir)
        self.klittra_core = KlittraCore(self.font_path, self.klittra_db_path, self.temp_dir)

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
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS checkin (
                        user_id TEXT NOT NULL,
                        checkin_date TEXT NOT NULL,
                        deer_count INTEGER NOT NULL,
                        PRIMARY KEY (user_id, checkin_date)
                    )
                ''')
                await conn.commit()
            logger.info("鹿打卡数据库初始化成功。")

            # 初始化扣日历数据库
            async with aiosqlite.connect(self.klittra_db_path) as conn:
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS klittra_checkin (
                        user_id TEXT NOT NULL,
                        checkin_date TEXT NOT NULL,
                        klittra_count INTEGER NOT NULL,
                        PRIMARY KEY (user_id, checkin_date)
                    )
                ''')
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )
                ''')
                await conn.commit()
            logger.info("扣日历数据库初始化成功。")
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}")

    def _get_adjusted_date(self, current_time: datetime) -> str:
        """根据配置的 day_start_time 获取调整后的日期字符串 (YYYY-MM-DD)"""
        # 解析HH:MM格式的时间
        try:
            hour, minute = map(int, self.day_start_time.split(':'))
            day_start_time = current_time.replace(hour=hour, minute=minute, second=0, microsecond=0)
        except (ValueError, AttributeError):
            # 如果格式不正确，默认使用00:00
            day_start_time = current_time.replace(hour=0, minute=0, second=0, microsecond=0)

        # 如果当前时间小于设置的每天开始时间，则认为是前一天
        if current_time.time() < day_start_time.time():
            adjusted_date = current_time - timedelta(days=1)
        else:
            adjusted_date = current_time
        return adjusted_date.strftime("%Y-%m-%d")

    async def _monthly_cleanup(self):
        """检查是否进入新月份，如果是则清空旧数据（根据配置决定）"""
        current_month = date.today().strftime("%Y-%m")
        try:
            # 清理鹿打卡数据库
            async with aiosqlite.connect(self.deer_db_path) as conn:
                cursor = await conn.execute("SELECT value FROM metadata WHERE key = 'last_cleanup_month'")
                last_cleanup = await cursor.fetchone()

                if not last_cleanup or last_cleanup[0] != current_month:
                    # 根据配置决定是否删除上月数据
                    if self.auto_delete_last_month_data:
                        await conn.execute("DELETE FROM checkin WHERE strftime('%Y-%m', checkin_date) != ?", (current_month,))
                        logger.info(f"已执行月度清理，删除了鹿打卡数据中非 {current_month} 的数据。")

                    await conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                                       ("last_cleanup_month", current_month))
                    await conn.commit()

            # 清理扣日历数据库
            async with aiosqlite.connect(self.klittra_db_path) as conn:
                cursor = await conn.execute("SELECT value FROM metadata WHERE key = 'last_cleanup_month_klittra'")
                last_cleanup = await cursor.fetchone()

                if not last_cleanup or last_cleanup[0] != current_month:
                    # 根据配置决定是否删除上月数据
                    if self.auto_delete_last_month_data:
                        await conn.execute("DELETE FROM klittra_checkin WHERE strftime('%Y-%m', checkin_date) != ?", (current_month,))
                        logger.info(f"已执行月度清理，删除了扣日历数据中非 {current_month} 的数据。")

                    await conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                                       ("last_cleanup_month_klittra", current_month))
                    await conn.commit()
        except Exception as e:
            logger.error(f"月度数据清理失败: {e}")

    @filter.regex(r'^🦌+$')
    async def handle_deer_checkin(self, event: AstrMessageEvent):
        """处理鹿打卡事件：记录数据，然后发送日历。"""
        # 检查群组白名单和用户黑名单
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if self.group_whitelist and int(group_id) not in self.group_whitelist:
            return  # 不在白名单中的群组不处理

        if user_id in self.user_blacklist:
            return  # 黑名单用户不处理

        await self._ensure_initialized()
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
                    cursor = await conn.execute('''
                        SELECT deer_count FROM checkin WHERE user_id = ? AND checkin_date = ?
                    ''', (user_id, today_str))
                    today_record = await cursor.fetchone()

                    current_daily_count = today_record[0] if today_record else 0
                    new_daily_count = current_daily_count + deer_count

                    if new_daily_count > self.daily_max_checkins:
                        yield event.plain_result(f"打卡失败！今日计入次数已达上限 {self.daily_max_checkins} 次。")
                        return

                # 查询当月打卡次数
                if self.monthly_max_checkins > 0:
                    current_month = today_str[:7]  # YYYY-MM 格式
                    # 查询本月其他日期的总次数
                    cursor = await conn.execute('''
                        SELECT SUM(deer_count) FROM checkin
                        WHERE user_id = ? AND strftime('%Y-%m', checkin_date) = ? AND checkin_date != ?
                    ''', (user_id, current_month, today_str))
                    monthly_record = await cursor.fetchone()

                    current_monthly_count = monthly_record[0] if monthly_record and monthly_record[0] is not None else 0

                    # 查询当天已有的数量
                    cursor = await conn.execute('''
                        SELECT deer_count FROM checkin WHERE user_id = ? AND checkin_date = ?
                    ''', (user_id, today_str))
                    today_record = await cursor.fetchone()
                    existing_count = today_record[0] if today_record and today_record[0] is not None else 0

                    # 计算打卡后的总数
                    new_monthly_count = current_monthly_count + existing_count + deer_count

                    if new_monthly_count > self.monthly_max_checkins:
                        yield event.plain_result(f"打卡失败！本月计入次数已达上限 {self.monthly_max_checkins} 次。")
                        return

        try:
            async with aiosqlite.connect(self.deer_db_path) as conn:
                await conn.execute('''
                    INSERT INTO checkin (user_id, checkin_date, deer_count)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, checkin_date)
                    DO UPDATE SET deer_count = deer_count + excluded.deer_count;
                ''', (user_id, today_str, deer_count))
                await conn.commit()
            logger.info(f"用户 {user_name} ({user_id}) 打卡成功，记录了 {deer_count} 个🦌。")
        except Exception as e:
            logger.error(f"记录用户 {user_name} ({user_id}) 的打卡数据失败: {e}")
            yield event.plain_result("打卡失败，数据库出错了 >_<")
            return

        async for result in self._generate_and_send_calendar(event):
            yield result

    @filter.regex(r'^🤏+$')
    async def handle_klittra_checkin(self, event: AstrMessageEvent):
        """处理扣日历记录事件：如果启用了扣日历功能，则记录数据并发送扣日历。"""
        # 检查是否启用了扣日历功能
        if not self.enable_female_calendar:
            return  # 未启用扣日历功能，不处理

        # 检查群组白名单和用户黑名单
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if self.group_whitelist and int(group_id) not in self.group_whitelist:
            return  # 不在白名单中的群组不处理

        if user_id in self.user_blacklist:
            return  # 黑名单用户不处理

        await self._ensure_initialized()
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
                    cursor = await conn.execute('''
                        SELECT klittra_count FROM klittra_checkin WHERE user_id = ? AND checkin_date = ?
                    ''', (user_id, today_str))
                    today_record = await cursor.fetchone()

                    current_daily_count = today_record[0] if today_record else 0
                    new_daily_count = current_daily_count + pinch_count

                    if new_daily_count > self.daily_max_checkins:
                        yield event.plain_result(f"记录失败！今日计入次数已达上限 {self.daily_max_checkins} 次。")
                        return

                # 查询当月打卡次数
                if self.monthly_max_checkins > 0:
                    current_month = today_str[:7]  # YYYY-MM 格式
                    # 查询本月其他日期的总次数
                    cursor = await conn.execute('''
                        SELECT SUM(klittra_count) FROM klittra_checkin
                        WHERE user_id = ? AND strftime('%Y-%m', checkin_date) = ? AND checkin_date != ?
                    ''', (user_id, current_month, today_str))
                    monthly_record = await cursor.fetchone()

                    current_monthly_count = monthly_record[0] if monthly_record and monthly_record[0] is not None else 0

                    # 查询当天已有的数量
                    cursor = await conn.execute('''
                        SELECT klittra_count FROM klittra_checkin WHERE user_id = ? AND checkin_date = ?
                    ''', (user_id, today_str))
                    today_record = await cursor.fetchone()
                    existing_count = today_record[0] if today_record and today_record[0] is not None else 0

                    # 计算打卡后的总数
                    new_monthly_count = current_monthly_count + existing_count + pinch_count

                    if new_monthly_count > self.monthly_max_checkins:
                        yield event.plain_result(f"记录失败！本月计入次数已达上限 {self.monthly_max_checkins} 次。")
                        return

        try:
            async with aiosqlite.connect(self.klittra_db_path) as conn:
                await conn.execute('''
                    INSERT INTO klittra_checkin (user_id, checkin_date, klittra_count)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, checkin_date)
                    DO UPDATE SET klittra_count = klittra_count + excluded.klittra_count;
                ''', (user_id, today_str, pinch_count))
                await conn.commit()
            logger.info(f"用户 {user_name} ({user_id}) 扣日历记录成功，记录了 {pinch_count} 个🤏。")
        except Exception as e:
            logger.error(f"记录用户 {user_name} ({user_id}) 的扣日历数据失败: {e}")
            yield event.plain_result("扣日历记录失败，数据库出错了 >_<")
            return

        # 发送扣日历
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()

        result_text, image_path, has_error = await self.klittra_core._generate_and_send_klittra_calendar(
            event, user_id, user_name, self.klittra_db_path
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

    @filter.regex(r'^🦌日历$')
    async def handle_calendar_command(self, event: AstrMessageEvent):
        """'🦌日历' 命令，只查询并发送用户的当月打卡日历。"""
        # 检查群组白名单和用户黑名单
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if self.group_whitelist and int(group_id) not in self.group_whitelist:
            return  # 不在白名单中的群组不处理

        if user_id in self.user_blacklist:
            return  # 黑名单用户不处理

        await self._ensure_initialized()
        user_name = event.get_sender_name()
        logger.info(f"用户 {user_name} ({event.get_sender_id()}) 使用命令查询日历。")

        async for result in self._generate_and_send_calendar(event):
            yield result

    @filter.regex(r'^🤏日历$')
    async def handle_klittra_calendar_command(self, event: AstrMessageEvent):
        """'🤏日历' 命令，只查询并发送用户的当月扣日历。"""
        # 检查是否启用了扣日历功能
        if not self.enable_female_calendar:
            return  # 未启用扣日历功能，不处理

        # 检查群组白名单和用户黑名单
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if self.group_whitelist and int(group_id) not in self.group_whitelist:
            return  # 不在白名单中的群组不处理

        if user_id in self.user_blacklist:
            return  # 黑名单用户不处理

        await self._ensure_initialized()
        user_name = event.get_sender_name()
        current_year = date.today().year
        current_month = date.today().month
        current_month_str = date.today().strftime("%Y-%m")

        checkin_records = {}
        total_deer_this_month = 0
        try:
            async with aiosqlite.connect(self.klittra_db_path) as conn:
                async with conn.execute(
                    "SELECT checkin_date, klittra_count FROM klittra_checkin WHERE user_id = ? AND strftime('%Y-%m', checkin_date) = ?",
                    (user_id, current_month_str)
                ) as cursor:
                    rows = await cursor.fetchall()
                    if not rows:
                        yield event.plain_result("您本月还没有扣日历记录哦，发送“🤏”开始第一次记录吧！")
                        return

                    for row in rows:
                        day = int(row[0].split('-')[2])
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
                total_deer_this_month
            )
            yield event.image_result(image_path)
        except FileNotFoundError:
            logger.error(f"字体文件未找到！无法生成扣日历图片。")
            yield event.plain_result(
                f"服务器缺少字体文件，无法生成扣日历图片。本月您已扣了{len(checkin_records)}天，累计{total_deer_this_month}次。")
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

    @filter.regex(r'^🦌补签\s+(\d{1,2})(?:\s+(\d+))?\s*$')
    async def handle_retro_checkin(self, event: AstrMessageEvent):
        """
        处理补签命令，格式: '🦌补签 <日期> [次数]'
        """
        # 检查群组白名单和用户黑名单
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if self.group_whitelist and int(group_id) not in self.group_whitelist:
            return  # 不在白名单中的群组不处理

        if user_id in self.user_blacklist:
            return  # 黑名单用户不处理

        await self._ensure_initialized()

        # 在函数内部，对消息原文进行正则搜索
        pattern = r'^🦌补签\s+(\d{1,2})(?:\s+(\d+))?\s*$'
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
            yield event.plain_result("命令格式不正确，请使用：🦌补签 日期 [次数] (例如：🦌补签 1 5 或 🦌补签 1)")
            return

        # 验证日期有效性
        today = date.today()
        current_year = today.year
        current_month = today.month

        days_in_month = calendar.monthrange(current_year, current_month)[1]

        if not (1 <= day_to_checkin <= days_in_month):
            yield event.plain_result(f"日期无效！本月（{current_month}月）只有 {days_in_month} 天。")
            return

        if day_to_checkin > today.day:
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
                    cursor = await conn.execute('''
                        SELECT deer_count FROM checkin WHERE user_id = ? AND checkin_date = ?
                    ''', (user_id, target_date_str))
                    today_record = await cursor.fetchone()

                    current_daily_count = today_record[0] if today_record else 0
                    new_daily_count = current_daily_count + deer_count

                    if new_daily_count > self.daily_max_checkins:
                        yield event.plain_result(f"补签失败！{target_date_str} 当日计入次数已达上限 {self.daily_max_checkins} 次。")
                        return

                # 查询当月打卡次数
                if self.monthly_max_checkins > 0:
                    current_month = target_date_str[:7]  # YYYY-MM 格式
                    # 查询本月其他日期的总次数
                    cursor = await conn.execute('''
                        SELECT SUM(deer_count) FROM checkin
                        WHERE user_id = ? AND strftime('%Y-%m', checkin_date) = ? AND checkin_date != ?
                    ''', (user_id, current_month, target_date_str))
                    monthly_record = await cursor.fetchone()

                    current_monthly_count = monthly_record[0] if monthly_record and monthly_record[0] is not None else 0

                    # 查询目标日期已有的数量
                    cursor = await conn.execute('''
                        SELECT deer_count FROM checkin WHERE user_id = ? AND checkin_date = ?
                    ''', (user_id, target_date_str))
                    today_record = await cursor.fetchone()
                    existing_count = today_record[0] if today_record and today_record[0] is not None else 0

                    # 计算补签后的总数
                    new_monthly_count = current_monthly_count + existing_count + deer_count

                    if new_monthly_count > self.monthly_max_checkins:
                        yield event.plain_result(f"补签失败！本月计入次数已达上限 {self.monthly_max_checkins} 次。")
                        return

        try:
            async with aiosqlite.connect(self.deer_db_path) as conn:
                await conn.execute('''
                    INSERT INTO checkin (user_id, checkin_date, deer_count)
                    VALUES (?, ?, ?)
                    ON CONFLICT(user_id, checkin_date)
                    DO UPDATE SET deer_count = deer_count + excluded.deer_count;
                ''', (user_id, target_date_str, deer_count))
                await conn.commit()
            logger.info(f"用户 {user_name} ({user_id}) 成功为 {target_date_str} 补签了 {deer_count} 个🦌。")
        except Exception as e:
            logger.error(f"为用户 {user_name} ({user_id}) 补签失败: {e}")
            yield event.plain_result("补签失败，数据库出错了 >_<")
            return

        # 发送成功提示，并返回更新后的日历图片
        yield event.plain_result(f"补签成功！已为 {current_month}月{day_to_checkin}日 增加了 {deer_count} 个鹿。")
        async for result in self._generate_and_send_calendar(event):
            yield result

    @filter.regex(r'^🦌撤销\s+(\d{1,2})(?:\s+(\d+))?\s*$')
    async def handle_undo_checkin(self, event: AstrMessageEvent):
        """
        处理撤销命令，格式: '🦌撤销 <日期> [次数]'
        """
        # 检查群组白名单和用户黑名单
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if self.group_whitelist and int(group_id) not in self.group_whitelist:
            return  # 不在白名单中的群组不处理

        if user_id in self.user_blacklist:
            return  # 黑名单用户不处理

        await self._ensure_initialized()

        # 在函数内部，对消息原文进行正则搜索
        pattern = r'^🦌撤销\s+(\d{1,2})(?:\s+(\d+))?\s*$'
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
            yield event.plain_result("命令格式不正确，请使用：🦌撤销 日期 [次数] (例如：🦌撤销 1 5 或 🦌撤销 1)")
            return

        # 验证日期有效性
        today = date.today()
        current_year = today.year
        current_month = today.month

        days_in_month = calendar.monthrange(current_year, current_month)[1]

        if not (1 <= day_to_checkin <= days_in_month):
            yield event.plain_result(f"日期无效！本月（{current_month}月）只有 {days_in_month} 天。")
            return

        if day_to_checkin > today.day:
            yield event.plain_result("抱歉，不能对未来进行撤销哦！")
            return

        # 数据库操作
        target_date = date(current_year, current_month, day_to_checkin)
        target_date_str = target_date.strftime("%Y-%m-%d")

        try:
            async with aiosqlite.connect(self.deer_db_path) as conn:
                # 查询当前记录
                cursor = await conn.execute("SELECT deer_count FROM checkin WHERE user_id = ? AND checkin_date = ?", (user_id, target_date_str))
                record = await cursor.fetchone()
                
                if not record or record[0] < deer_count:
                    current_count = record[0] if record else 0
                    yield event.plain_result(f"撤销失败！{target_date_str} 的打卡次数仅为 {current_count}，不足以减少 {deer_count} 次。")
                    return
                
                # 执行更新
                new_count = record[0] - deer_count
                if new_count == 0:
                    await conn.execute("DELETE FROM checkin WHERE user_id = ? AND checkin_date = ?", (user_id, target_date_str))
                else:
                    await conn.execute("UPDATE checkin SET deer_count = ? WHERE user_id = ? AND checkin_date = ?", (new_count, user_id, target_date_str))
                await conn.commit()
            
            logger.info(f"用户 {user_name} ({user_id}) 成功为 {target_date_str} 撤销了 {deer_count} 个🦌。")
        except Exception as e:
            logger.error(f"为用户 {user_name} ({user_id}) 撤销失败: {e}")
            yield event.plain_result("撤销失败，数据库出错了 >_<")
            return

        # 发送成功提示，并返回更新后的日历图片
        yield event.plain_result(f"撤销成功！已为 {current_month}月{day_to_checkin}日 减少了 {deer_count} 个鹿。")
        async for result in self._generate_and_send_calendar(event):
            yield result

    @filter.regex(r'^🦌生涯$')
    async def handle_deer_career(self, event: AstrMessageEvent):
        """
        响应 '🦌生涯' 命令，生成并发送用户的生涯统计报告。
        """
        # 检查群组白名单和用户黑名单
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if self.group_whitelist and int(group_id) not in self.group_whitelist:
            return  # 不在白名单中的群组不处理

        if user_id in self.user_blacklist:
            return  # 黑名单用户不处理

        await self._ensure_initialized()
        user_name = event.get_sender_name()
        
        # 获取当前调整后的日期
        current_time = datetime.now()
        today_str = self._get_adjusted_date(current_time)
        today_date = datetime.strptime(today_str, "%Y-%m-%d").date()

        try:
            async with aiosqlite.connect(self.deer_db_path) as conn:
                # 1. 基础数据查询
                cursor = await conn.execute('''
                    SELECT 
                        MIN(checkin_date), 
                        COUNT(DISTINCT checkin_date), 
                        SUM(deer_count),
                        MAX(checkin_date)
                    FROM checkin WHERE user_id = ?
                ''', (user_id,))
                first_date_str, total_days, total_count, last_date_str = await cursor.fetchone()

                if not first_date_str:
                    yield event.plain_result("您还没有任何打卡记录，发送“🦌”开始您的生涯吧！")
                    return

                first_date = datetime.strptime(first_date_str, "%Y-%m-%d").date()
                total_span_days = (today_date - first_date).days + 1  # 累计时光
                
                # 日均发射 (按总跨度天数)
                daily_avg = total_count / total_span_days if total_span_days else 0
                
                # 活跃占比 (动手天数 / 生涯总天数)
                active_ratio = (total_days / total_span_days) * 100 if total_span_days else 0

                # 2. 巅峰时刻 & 贤者时期
                # 先获取所有月份的数据，找出 0 记录的月份
                cursor = await conn.execute('''
                    SELECT strftime('%Y-%m', checkin_date) as month, SUM(deer_count) as total 
                    FROM checkin WHERE user_id = ? 
                    GROUP BY month
                ''', (user_id,))
                month_data = await cursor.fetchall()
                month_dict = {row[0]: row[1] for row in month_data}

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
                
                # 重新计算月度之最 (其实DB查出来的就是最大的，除非全是0)
                if full_month_data:
                    full_month_data.sort(key=lambda x: x[1], reverse=True)
                    max_month_str, max_month_count = full_month_data[0]
                    
                    full_month_data.sort(key=lambda x: x[1]) # 升序
                    min_month_str, min_month_count = full_month_data[0] # 最小的 (可能是0)
                else:
                    max_month_str, max_month_count = "N/A", 0
                    min_month_str, min_month_count = "N/A", 0

                # 单日之最
                cursor = await conn.execute('''
                    SELECT checkin_date, deer_count FROM checkin 
                    WHERE user_id = ? ORDER BY deer_count DESC, checkin_date DESC LIMIT 1
                ''', (user_id,))
                max_day_record = await cursor.fetchone()
                max_day_date = max_day_record[0]
                max_day_count = max_day_record[1]

                # 最长休养期 (连续未打卡)
                # 获取所有打卡日期并排序
                cursor = await conn.execute('''
                    SELECT DISTINCT checkin_date FROM checkin WHERE user_id = ? ORDER BY checkin_date ASC
                ''', (user_id,))
                all_dates = await cursor.fetchall()
                date_objs = [datetime.strptime(row[0], "%Y-%m-%d").date() for row in all_dates]

                max_gap = 0
                gap_start = None
                gap_end = None

                # 检查所有间隔
                for i in range(len(date_objs) - 1):
                    d1 = date_objs[i]
                    d2 = date_objs[i+1]
                    gap = (d2 - d1).days - 1
                    if gap > max_gap:
                        max_gap = gap
                        gap_start = d1 + timedelta(days=1)
                        gap_end = d2 - timedelta(days=1)
                
                # 还要检查最后一次打卡到今天的间隔 (如果今天没打卡)
                last_checkin_date = datetime.strptime(last_date_str, "%Y-%m-%d").date()
                days_since_last = (today_date - last_checkin_date).days
                
                if days_since_last > 0:
                    current_gap = days_since_last # 昨天也没打的话
                    # days_since_last = 1 (昨天打卡), gap = 0
                    if days_since_last - 1 > max_gap:
                        max_gap = days_since_last - 1
                        gap_start = last_checkin_date + timedelta(days=1)
                        gap_end = today_date - timedelta(days=1) # 直到昨天

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
            "summary_comment": summary_comment
        }

        # 生成图片
        image_path = ""
        try:
            image_path = await asyncio.to_thread(
                self.deer_core._create_career_image,
                user_name,
                stats
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

    @filter.regex(r'^🦌排行$')
    async def handle_deer_ranking(self, event: AstrMessageEvent):
        """
        响应 '鹿排行' 命令，生成并发送当前月度的打卡排行榜图片。
        """
        # 检查是否在群聊中
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在群聊中使用此功能！")
            return

        user_id = event.get_sender_id()

        if self.group_whitelist and int(group_id) not in self.group_whitelist:
            logger.info(f"群 {group_id} 不在白名单中，忽略请求")
            return  # 不在白名单中的群组不处理

        if user_id in self.user_blacklist:
            logger.info(f"用户 {user_id} 在黑名单中，忽略请求")
            return  # 黑名单用户不处理

        await self._ensure_initialized()
        current_year = date.today().year
        current_month = date.today().month
        current_month_str = date.today().strftime("%Y-%m")

        logger.info(f"开始查询群 {group_id} 的 {current_month_str} 月排行榜数据")

        # 查询当月所有用户的打卡数据
        all_users_data = []
        try:
            async with aiosqlite.connect(self.deer_db_path) as conn:
                async with conn.execute(
                    "SELECT user_id, SUM(deer_count) as total_deer FROM checkin WHERE strftime('%Y-%m', checkin_date) = ? GROUP BY user_id ORDER BY total_deer DESC",
                    (current_month_str,)
                ) as cursor:
                    rows = await cursor.fetchall()
                    for row in rows:
                        user_id, total_deer = row
                        all_users_data.append((user_id, total_deer))
            logger.info(f"查询到 {len(all_users_data)} 个用户的打卡数据")
        except Exception as e:
            logger.error(f"查询当月排行榜数据失败: {e}")
            yield event.plain_result("查询排行榜数据时出错了 >_<")
            return

        if not all_users_data:
            logger.info("本月没有任何打卡记录")
            yield event.plain_result("本月还没有任何打卡记录哦，快发送“🦌”开始打卡吧！")
            return

        # 获取当前群的所有成员
        try:
            group_members = await self._get_group_members(event, group_id)
            if not group_members:
                logger.warning(f"无法获取群 {group_id} 的成员列表")
                yield event.plain_result("无法获取群成员信息，无法生成排行榜。")
                return
        except Exception as e:
            logger.error(f"获取群成员列表失败: {e}")
            yield event.plain_result("获取群成员信息时出错了 >_<")
            return

        # 调试信息：显示当前用户是否在群成员中
        group_user_ids = {str(member['user_id']) for member in group_members}  # 确保转换为字符串

        # 过滤出当前群的用户
        ranking_data = [(user_id, deer_count) for user_id, deer_count in all_users_data if str(user_id) in group_user_ids]

        # 根据配置的每月上限过滤数据（如果设置了限制）
        if self.monthly_max_checkins > 0:
            ranking_data = [(user_id, deer_count) for user_id, deer_count in ranking_data if deer_count <= self.monthly_max_checkins]

        # 只取前self.ranking_display_count名（默认10名）
        ranking_display_count = getattr(self, 'ranking_display_count', 10)  # 默认显示10名
        ranking_data = ranking_data[:ranking_display_count]

        if not ranking_data:
            logger.info(f"群 {group_id} 中本月没有用户有打卡记录，所有 {len(all_users_data)} 个有记录的用户都不在群中或超过限制")
            yield event.plain_result("本月本群还没有任何打卡记录哦，快发送“🦌”开始打卡吧！")
            return

        # 获取用户昵称
        user_names = []
        for user_id, _ in ranking_data:
            try:
                user_name = await self._get_user_name(event, user_id)
                user_names.append(user_name)
            except Exception:
                user_names.append(f"用户{user_id}")

        # 生成排行榜图片
        image_path = ""
        try:
            image_path = await asyncio.to_thread(
                self._create_ranking_image,
                user_names,
                ranking_data,
                current_year,
                current_month
            )
            yield event.image_result(image_path)
        except FileNotFoundError:
            logger.error(f"字体文件未找到！无法生成排行榜图片。")
            ranking_text = f"🦌{current_year}年{current_month}月打卡排行榜:\n"
            for i, (user_name, deer_count) in enumerate(zip(user_names, [data[1] for data in ranking_data]), 1):
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

    @filter.regex(r'^🦌(?:分析|报告)(?:\s+(\d{2}|\d{4}))?$')
    async def handle_analysis(self, event: AstrMessageEvent):
        """
        响应 '🦌分析' 命令，生成并发送打卡分析报告。
        不带参数：分析本月数据
        两位数字：分析指定月份数据
        四位数字：分析指定年份数据
        """
        # 检查群组白名单和用户黑名单
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if self.group_whitelist and int(group_id) not in self.group_whitelist:
            return  # 不在白名单中的群组不处理

        if user_id in self.user_blacklist:
            return  # 黑名单用户不处理

        await self._ensure_initialized()
        pattern = r'^🦌(?:分析|报告)(?:\s+(\d{2}|\d{4}))?$'
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
            period_data = await self._get_user_period_data(user_id, target_year, target_month)

            # 生成分析报告
            analysis_result, checkin_rate = await self._generate_monthly_analysis_report(
                user_name, target_year, target_month, period_data
            )

        elif len(param) == 2:  # 月份
            try:
                target_month = int(param)
                if not (1 <= target_month <= 12):
                    yield event.plain_result("月份必须在1-12之间哦！")
                    return
            except ValueError:
                yield event.plain_result("请输入正确的月份数字！")
                return

            # 计算年份
            current_date = datetime.now()
            current_month = current_date.month
            current_year = current_date.year

            if target_month > current_month:
                target_year = current_year - 1
            else:
                target_year = current_year

            target_period = f"{target_year}年{target_month}月"

            # 查询指定月份数据
            period_data = await self._get_user_period_data(user_id, target_year, target_month)

            # 生成分析报告
            analysis_result, checkin_rate = await self._generate_monthly_analysis_report(
                user_name, target_year, target_month, period_data
            )

        elif len(param) == 4:  # 年份
            try:
                target_year = int(param)
                current_year = datetime.now().year
                if target_year > current_year:
                    yield event.plain_result("年份不能超过当前年份哦！")
                    return
            except ValueError:
                yield event.plain_result("请输入正确的年份数字！")
                return

            target_period = f"{target_year}年"

            # 查询指定年份数据
            yearly_data = await self._get_user_yearly_data(user_id, target_year)

            # 生成年份分析报告
            analysis_result = await self._generate_yearly_analysis_report(
                user_name, target_year, yearly_data
            )
        else:
            yield event.plain_result("命令格式错误，请使用：🦌分析 [月份/年份]（如：🦌分析、🦌分析 11、🦌分析 2025）")
            return

        logger.info(f"用户 {user_name} ({user_id}) 请求查看 {target_period} 的分析报告。")

        if not analysis_result:
            yield event.plain_result(f"您在{target_period}还没有打卡记录哦，发送“🦌”开始打卡吧！")
            return

        # 生成并发送分析图片
        image_path = ""
        try:
            image_path = await asyncio.to_thread(
                self._create_analysis_image,
                user_name,
                target_period,
                analysis_result,
                checkin_rate if 'checkin_rate' in locals() else 0.0
            )
            yield event.image_result(image_path)
        except FileNotFoundError:
            logger.error(f"字体文件未找到！无法生成分析图片。")
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

    @filter.regex(r'^🦌年历(?:\s+(\d{4}))?$')
    async def handle_yearly_calendar(self, event: AstrMessageEvent):
        """
        响应 '🦌年历' 命令，生成并发送指定年份的完整打卡日历图片。
        不带参数默认当年。
        """
        # 检查群组白名单和用户黑名单
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if self.group_whitelist and int(group_id) not in self.group_whitelist:
            return  # 不在白名单中的群组不处理

        if user_id in self.user_blacklist:
            return  # 黑名单用户不处理

        await self._ensure_initialized()

        # 解析年份参数
        pattern = r'^🦌年历(?:\s+(\d{4}))?$'
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
                    (user_id, str(current_year))
                ) as cursor:
                    rows = await cursor.fetchall()
                    if not rows:
                        yield event.plain_result(f"您在{current_year}年还没有打卡记录哦，发送“🦌”开始打卡吧！")
                        return

                    for row in rows:
                        date_str = row[0]
                        count = row[1]
                        year, month, day = date_str.split('-')
                        month = int(month)
                        day = int(day)

                        if month not in yearly_data:
                            yearly_data[month] = {}
                        yearly_data[month][day] = count
        except Exception as e:
            logger.error(f"查询用户 {user_name} ({user_id}) 的 {current_year}年数据失败: {e}")
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
                yearly_data
            )
            yield event.image_result(image_path)
        except FileNotFoundError:
            logger.error(f"字体文件未找到！无法生成年历图片。")
            # 生成文本总结
            total_months = len(yearly_data)
            total_days = sum(len(days) for days in yearly_data.values())
            total_deer = sum(sum(days.values()) for days in yearly_data.values())
            yield event.plain_result(
                f"服务器缺少字体文件，无法生成年历图片。{current_year}年您已打卡{total_months}个月，{total_days}天，累计{total_deer}个🦌。")
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

    @filter.regex(r'^🤏年历(?:\s+(\d{4}))?$')
    async def handle_klittra_yearly_calendar(self, event: AstrMessageEvent):
        """
        响应 '🤏年历' 命令，生成并发送指定年份的完整扣日历图片。
        不带参数默认当年。
        """
        # 检查是否启用了扣日历功能
        if not self.enable_female_calendar:
            return  # 未启用扣日历功能，不处理

        # 检查群组白名单和用户黑名单
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if self.group_whitelist and int(group_id) not in self.group_whitelist:
            return  # 不在白名单中的群组不处理

        if user_id in self.user_blacklist:
            return  # 黑名单用户不处理

        await self._ensure_initialized()

        # 解析年份参数
        pattern = r'^🤏年历(?:\s+(\d{4}))?$'
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
                    (user_id, str(current_year))
                ) as cursor:
                    rows = await cursor.fetchall()
                    if not rows:
                        yield event.plain_result(f"您在{current_year}年还没有扣日历记录哦，发送“🤏”开始记录吧！")
                        return

                    for row in rows:
                        date_str = row[0]
                        count = row[1]
                        year, month, day = date_str.split('-')
                        month = int(month)
                        day = int(day)

                        if month not in yearly_data:
                            yearly_data[month] = {}
                        yearly_data[month][day] = count
        except Exception as e:
            logger.error(f"查询用户 {user_name} ({user_id}) 的 {current_year}年扣日历数据失败: {e}")
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
                yearly_data
            )
            yield event.image_result(image_path)
        except FileNotFoundError:
            logger.error(f"字体文件未找到！无法生成扣日历图片。")
            # 生成文本总结
            total_months = len(yearly_data)
            total_days = sum(len(days) for days in yearly_data.values())
            total_deer = sum(sum(days.values()) for days in yearly_data.values())
            yield event.plain_result(
                f"服务器缺少字体文件，无法生成扣日历图片。{current_year}年您已扣了{total_months}个月，{total_days}天，共{total_deer}次。")
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

    @filter.regex(r'^🦌月历\s+(\d{1,2})$')
    async def handle_specific_month_calendar(self, event: AstrMessageEvent):
        """
        响应 '🦌月历 X' 命令，生成并发送指定月份的打卡日历图片。
        """
        # 检查群组白名单和用户黑名单
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if self.group_whitelist and int(group_id) not in self.group_whitelist:
            return  # 不在白名单中的群组不处理

        if user_id in self.user_blacklist:
            return  # 黑名单用户不处理

        await self._ensure_initialized()

        pattern = r'^🦌月历\s+(\d{1,2})$'
        match = re.search(pattern, event.message_str)
        if not match:
            yield event.plain_result("命令格式错误，请使用：🦌月历 月份（如：🦌月历 11）")
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

        logger.info(f"用户 {user_name} ({user_id}) 请求查看 {target_year}年{target_month}月的日历。")

        # 查询指定月份的打卡记录
        checkin_records = {}
        total_deer_this_month = 0
        try:
            async with aiosqlite.connect(self.deer_db_path) as conn:
                async with conn.execute(
                    "SELECT checkin_date, deer_count FROM checkin WHERE user_id = ? AND strftime('%Y-%m', checkin_date) = ?",
                    (user_id, target_month_str)
                ) as cursor:
                    rows = await cursor.fetchall()
                    if not rows:
                        yield event.plain_result(f"您在{target_year}年{target_month}月还没有打卡记录哦，发送“🦌”开始打卡吧！")
                        return

                    for row in rows:
                        day = int(row[0].split('-')[2])
                        count = row[1]
                        checkin_records[day] = count
                        total_deer_this_month += count
        except Exception as e:
            logger.error(f"查询用户 {user_name} ({user_id}) 的 {target_year}年{target_month}月数据失败: {e}")
            yield event.plain_result("查询月历数据时出错了 >_<")
            return

        # 生成并发送日历图片
        image_path = ""
        try:
            image_path = await asyncio.to_thread(
                self._create_calendar_image,
                user_id,
                user_name,
                target_year,
                target_month,
                checkin_records,
                total_deer_this_month
            )
            yield event.image_result(image_path)
        except FileNotFoundError:
            logger.error(f"字体文件未找到！无法生成日历图片。")
            yield event.plain_result(
                f"服务器缺少字体文件，无法生成日历图片。{target_year}年{target_month}月您已打卡{len(checkin_records)}天，累计{total_deer_this_month}个🦌。")
        except Exception as e:
            logger.error(f"生成或发送日历图片失败: {e}")
            yield event.plain_result("处理日历图片时发生了未知错误 >_<")
        finally:
            if image_path and os.path.exists(image_path):
                try:
                    await asyncio.to_thread(os.remove, image_path)
                    logger.debug(f"已成功删除临时图片: {image_path}")
                except OSError as e:
                    logger.error(f"删除临时图片 {image_path} 失败: {e}")

    @filter.regex(r'^🤏月历\s+(\d{1,2})$')
    async def handle_klittra_specific_month_calendar(self, event: AstrMessageEvent):
        """
        响应 '🤏月历 X' 命令，生成并发送指定月份的扣日历图片。
        """
        # 检查是否启用了扣日历功能
        if not self.enable_female_calendar:
            return  # 未启用扣日历功能，不处理

        # 检查群组白名单和用户黑名单
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if self.group_whitelist and int(group_id) not in self.group_whitelist:
            return  # 不在白名单中的群组不处理

        if user_id in self.user_blacklist:
            return  # 黑名单用户不处理

        await self._ensure_initialized()

        pattern = r'^🤏月历\s+(\d{1,2})$'
        match = re.search(pattern, event.message_str)
        if not match:
            yield event.plain_result("命令格式错误，请使用：🤏月历 月份（如：🤏月历 11）")
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

        logger.info(f"用户 {user_name} ({user_id}) 请求查看 {target_year}年{target_month}月的扣日历。")

        # 查询指定月份的扣日历记录
        checkin_records = {}
        total_deer_this_month = 0
        try:
            async with aiosqlite.connect(self.klittra_db_path) as conn:
                async with conn.execute(
                    "SELECT checkin_date, klittra_count FROM klittra_checkin WHERE user_id = ? AND strftime('%Y-%m', checkin_date) = ?",
                    (user_id, target_month_str)
                ) as cursor:
                    rows = await cursor.fetchall()
                    if not rows:
                        yield event.plain_result(f"您在{target_year}年{target_month}月还没有扣日历记录哦，发送“🤏”开始记录吧！")
                        return

                    for row in rows:
                        day = int(row[0].split('-')[2])
                        count = row[1]
                        checkin_records[day] = count
                        total_deer_this_month += count
        except Exception as e:
            logger.error(f"查询用户 {user_name} ({user_id}) 的 {target_year}年{target_month}月扣日历数据失败: {e}")
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
                total_deer_this_month
            )
            yield event.image_result(image_path)
        except FileNotFoundError:
            logger.error(f"字体文件未找到！无法生成扣日历图片。")
            yield event.plain_result(
                f"服务器缺少字体文件，无法生成扣日历图片。{target_year}年{target_month}月您已扣了{len(checkin_records)}天，共{total_deer_this_month}次。")
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

    @filter.regex(r'^🤏排行$')
    async def handle_klittra_ranking(self, event: AstrMessageEvent):
        """
        响应 '扣日历排行' 命令，生成并发送当前月度的扣日历排行榜图片。
        """
        # 检查是否在群聊中
        group_id = event.get_group_id()
        if not group_id:
            yield event.plain_result("请在群聊中使用此功能！")
            return

        user_id = event.get_sender_id()

        if self.group_whitelist and int(group_id) not in self.group_whitelist:
            logger.info(f"群 {group_id} 不在白名单中，忽略请求")
            return  # 不在白名单中的群组不处理

        if user_id in self.user_blacklist:
            logger.info(f"用户 {user_id} 在黑名单中，忽略请求")
            return  # 黑名单用户不处理

        await self._ensure_initialized()
        current_year = date.today().year
        current_month = date.today().month
        current_month_str = date.today().strftime("%Y-%m")

        logger.info(f"开始查询群 {group_id} 的 {current_month_str} 月扣日历排行榜数据")

        # 查询当月所有用户的扣日历数据
        all_users_data = []
        try:
            async with aiosqlite.connect(self.klittra_db_path) as conn:
                async with conn.execute(
                    "SELECT user_id, SUM(klittra_count) as total_klittra FROM klittra_checkin WHERE strftime('%Y-%m', checkin_date) = ? GROUP BY user_id ORDER BY total_klittra DESC",
                    (current_month_str,)
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
            yield event.plain_result("本月还没有任何扣日历记录哦，快发送“🤏”开始扣日历吧！")
            return

        # 获取当前群的所有成员
        try:
            group_members = await self._get_group_members(event, group_id)
            if not group_members:
                logger.warning(f"无法获取群 {group_id} 的成员列表")
                yield event.plain_result("无法获取群成员信息，无法生成扣日历排行榜。")
                return
        except Exception as e:
            logger.error(f"获取群成员列表失败: {e}")
            yield event.plain_result("获取群成员信息时出错了 >_<")
            return

        # 调试信息：显示当前用户是否在群成员中
        group_user_ids = {str(member['user_id']) for member in group_members}  # 确保转换为字符串

        # 过滤出当前群的用户
        ranking_data = [(user_id, klittra_count) for user_id, klittra_count in all_users_data if str(user_id) in group_user_ids]

        # 根据配置的每月上限过滤数据（如果设置了限制）
        if self.monthly_max_checkins > 0:
            ranking_data = [(user_id, klittra_count) for user_id, klittra_count in ranking_data if klittra_count <= self.monthly_max_checkins]

        # 只取前self.ranking_display_count名（默认10名）
        ranking_display_count = getattr(self, 'ranking_display_count', 10)  # 默认显示10名
        ranking_data = ranking_data[:ranking_display_count]

        if not ranking_data:
            logger.info(f"群 {group_id} 中本月没有用户有扣日历记录，所有 {len(all_users_data)} 个有记录的用户都不在群中或超过限制")
            yield event.plain_result("本月本群还没有任何扣日历记录哦，快发送“🤏”开始扣日历吧！")
            return

        # 获取用户昵称
        user_names = []
        for user_id, _ in ranking_data:
            try:
                user_name = await self._get_user_name(event, user_id)
                user_names.append(user_name)
            except Exception:
                user_names.append(f"用户{user_id}")

        # 生成排行榜图片
        image_path = ""
        try:
            image_path = await asyncio.to_thread(
                self.klittra_core._create_klittra_ranking_image,
                user_names,
                ranking_data,
                current_year,
                current_month
            )
            yield event.image_result(image_path)
        except FileNotFoundError:
            logger.error(f"字体文件未找到！无法生成扣日历排行榜图片。")
            ranking_text = f"🤏{current_year}年{current_month}月扣日历排行榜:\n"
            for i, (user_name, klittra_count) in enumerate(zip(user_names, [data[1] for data in ranking_data]), 1):
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

    @filter.regex(r'^🦌帮助$')
    async def handle_help_command(self, event: AstrMessageEvent):
        """
        响应 '🦌帮助' 命令，发送一个包含所有指令用法的菜单。
        """
        # 检查群组白名单和用户黑名单
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if self.group_whitelist and int(group_id) not in self.group_whitelist:
            return  # 不在白名单中的群组不处理

        if user_id in self.user_blacklist:
            return  # 黑名单用户不处理
        help_text = (
            "--- 🦌打卡帮助菜单 ---\n\n"
            "1️⃣  🦌打卡\n"
            "    ▸ 命令: 直接发送 🦌 (可发送多个)\n"
            "    ▸ 作用: 记录今天🦌的数量。\n"
            "    ▸ 示例: 🦌🦌🦌\n\n"
            "2️⃣  查看记录\n"
            "    ▸ 命令: 🦌日历\n"
            "    ▸ 作用: 查看您本月的打卡日历，不记录打卡。\n\n"
            "3️⃣  查看年度记录\n"
            "    ▸ 命令: 🦌年历 [年份]\n"
            "    ▸ 作用: 查看完整打卡日历。不带年份默认查看今年。\n"
            "    ▸ 示例: 🦌年历 2024\n\n"
            "4️⃣  查看生涯分析\n"
            "    ▸ 命令: 🦌生涯\n"
            "    ▸ 作用: 查看您的生涯统计、称号、最长记录等。\n\n"
            "5️⃣  查看指定月份记录\n"
            "    ▸ 命令: 🦌月历 月份数字\n"
            "    ▸ 作用: 查看指定月份的打卡日历，不记录打卡。\n"
            "    ▸ 示例: 🦌月历 11 (查看11月的日历)\n\n"
            "6️⃣  打卡分析\n"
            "    ▸ 命令: 🦌报告 [月份/年份]\n"
            "    ▸ 作用: 分析您的打卡数据并生成报告。\n"
            "    ▸ 示例: 🦌报告 (本月分析)、🦌报告 11 (11月分析)、🦌报告 2025 (2025年分析)\n\n"
            "7️⃣  补签\n"
            "    ▸ 命令: 🦌补签 [日期] [次数]\n"
            "    ▸ 作用: 为本月指定日期补上打卡记录。\n"
            "    ▸ 示例: 🦌补签 1 5 (为本月1号补签5次)，🦌补签 1 (为本月1号补签1次)\n\n"
            "8️⃣  撤销\n"
            "    ▸ 命令: 🦌撤销 [日期] [次数]\n"
            "    ▸ 作用: 为本月指定日期减少打卡记录。\n"
            "    ▸ 示例: 🦌撤销 1 5 (为本月1号减少5次)，🦌撤销 1 (为本月1号减少1次)\n\n"
            "9️⃣  显示此帮助\n"
            "    ▸ 命令: 🦌帮助\n\n"
            "祝您一🦌顺畅！"
        )

        yield event.plain_result(help_text)

    async def _get_group_members(self, event: AstrMessageEvent, group_id: str) -> list:
        """获取群成员列表"""
        return await self.deer_core._get_group_members(event, group_id)

    async def _get_user_name(self, event: AstrMessageEvent, user_id: str) -> str:
        """获取用户昵称"""
        return await self.deer_core._get_user_name(event, user_id)

    def _create_ranking_image(self, user_names: list, ranking_data: list, year: int, month: int) -> str:
        """
        绘制月度打卡排行榜图片，参考日历图片风格
        """
        return self.deer_core._create_ranking_image(user_names, ranking_data, year, month)

    async def _get_user_period_data(self, user_id: str, year: int, month: int) -> dict:
        """获取用户指定月份的打卡数据"""
        return await self.deer_core._get_user_period_data(user_id, year, month)

    async def _get_user_yearly_data(self, user_id: str, year: int) -> dict:
        """获取用户指定年份的打卡数据"""
        return await self.deer_core._get_user_yearly_data(user_id, year)

    async def _generate_monthly_analysis_report(self, user_name: str, year: int, month: int, period_data: dict) -> tuple[str, float]:
        """生成月度趣味打卡分析报告"""
        return await self.deer_core._generate_monthly_analysis_report(user_name, year, month, period_data)

    async def _generate_yearly_analysis_report(self, user_name: str, year: int, yearly_data: dict) -> str:
        """生成年度趣味打卡分析报告（无emoji版）"""
        return await self.deer_core._generate_yearly_analysis_report(user_name, year, yearly_data)

    def _create_analysis_image(self, user_name: str, target_period: str, analysis_result: str, checkin_rate: float = 0.0) -> str:
        """
        绘制分析报告图片
        """
        return self.deer_core._create_analysis_image(user_name, target_period, analysis_result, checkin_rate)

    def _wrap_text(self, text: str, font, max_width: int) -> list:
        """
        文本自动换行
        """
        return self.deer_core._wrap_text(text, font, max_width)

    def _create_yearly_calendar_image(self, user_id: str, user_name: str, year: int, yearly_data: dict) -> str:
        """
        绘制年度打卡日历图片，将12个月的日历按网格排列
        """
        return self.deer_core._create_yearly_calendar_image(user_id, user_name, year, yearly_data)

    def _create_calendar_image(self, user_id: str, user_name: str, year: int, month: int, checkin_data: dict, total_deer: int) -> str:
        """
        绘制用户月度打卡日历图片
        """
        return self.deer_core._create_calendar_image(user_id, user_name, year, month, checkin_data, total_deer)

    async def _generate_and_send_calendar(self, event: AstrMessageEvent):
        """查询和生成当月的打卡日历。"""
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()

        # 使用 deer_core 方法
        result_text, image_path, has_error = await self.deer_core._generate_and_send_calendar(
            event, user_id, user_name, self.deer_db_path
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
                yield event.plain_result("您本月还没有打卡记录哦，发送“🦌”开始第一次打卡吧！")

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