import aiosqlite
import calendar
from datetime import date
from PIL import Image, ImageDraw, ImageFont
import os
import re
import time
import asyncio
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core.star import StarTools

FONT_FILE = "font.ttf"
DB_NAME = "deer_checkin.db"


@register(
    "astrbot_plugin_deer_check",
    "DITF16",
    "一个发送'🦌'表情进行打卡并生成月度日历的插件",
    "1.0"
)
class DeerCheckinPlugin(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        data_dir = StarTools.get_data_dir("astrbot_plugin_deer_check")
        os.makedirs(data_dir, exist_ok=True)
        plugin_dir = os.path.dirname(__file__)
        resources_dir = os.path.join(plugin_dir, "resources")
        self.db_path = os.path.join(data_dir, DB_NAME)
        self.font_path = os.path.join(resources_dir, FONT_FILE)
        self.temp_dir = os.path.join(plugin_dir, "tmp")
        os.makedirs(self.temp_dir, exist_ok=True)

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
            async with aiosqlite.connect(self.db_path) as conn:
                await conn.execute('''
                    CREATE TABLE IF NOT EXISTS checkin (
                        user_id TEXT NOT NULL,
                        checkin_date TEXT NOT NULL,
                        deer_count INTEGER NOT NULL,
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
            logger.info("鹿打卡数据库初始化成功。")
        except Exception as e:
            logger.error(f"数据库初始化失败: {e}")

    async def _monthly_cleanup(self):
        """检查是否进入新月份，如果是则清空旧数据"""
        current_month = date.today().strftime("%Y-%m")
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                cursor = await conn.execute("SELECT value FROM metadata WHERE key = 'last_cleanup_month'")
                last_cleanup = await cursor.fetchone()

                if not last_cleanup or last_cleanup[0] != current_month:
                    await conn.execute("DELETE FROM checkin WHERE strftime('%Y-%m', checkin_date) != ?", (current_month,))
                    await conn.execute("INSERT OR REPLACE INTO metadata (key, value) VALUES (?, ?)",
                                       ("last_cleanup_month", current_month))
                    await conn.commit()
                    logger.info(f"已执行月度清理，现在是 {current_month}。")
        except Exception as e:
            logger.error(f"月度数据清理失败: {e}")

    @filter.regex(r'^🦌+$')
    async def handle_deer_checkin(self, event: AstrMessageEvent):
        """处理鹿打卡事件：记录数据，然后发送日历。"""
        await self._ensure_initialized()
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        deer_count = event.message_str.count("🦌")
        today_str = date.today().strftime("%Y-%m-%d")

        try:
            async with aiosqlite.connect(self.db_path) as conn:
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

    @filter.regex(r'^🦌日历$')
    async def handle_calendar_command(self, event: AstrMessageEvent):
        """'🦌日历' 命令，只查询并发送用户的当月打卡日历。"""
        await self._ensure_initialized()
        user_name = event.get_sender_name()
        logger.info(f"用户 {user_name} ({event.get_sender_id()}) 使用命令查询日历。")

        async for result in self._generate_and_send_calendar(event):
            yield result

    def _create_calendar_image(self, user_id: str, user_name: str, year: int, month: int, checkin_data: dict, total_deer: int) -> str:
        """
        绘制用户月度打卡日历图片
        """
        WIDTH, HEIGHT = 700, 620
        BG_COLOR = (255, 255, 255)
        HEADER_COLOR = (50, 50, 50)
        WEEKDAY_COLOR = (100, 100, 100)
        DAY_COLOR = (80, 80, 80)
        TODAY_BG_COLOR = (240, 240, 255)
        CHECKIN_MARK_COLOR = (0, 150, 50)
        DEER_COUNT_COLOR = (139, 69, 19)

        try:
            font_header = ImageFont.truetype(self.font_path, 32)
            font_weekday = ImageFont.truetype(self.font_path, 18)
            font_day = ImageFont.truetype(self.font_path, 20)
            font_check_mark = ImageFont.truetype(self.font_path, 28)
            font_deer_count = ImageFont.truetype(self.font_path, 16)
            font_summary = ImageFont.truetype(self.font_path, 18)
        except FileNotFoundError as e:
            logger.error(f"字体文件加载失败: {e}")
            raise e

        img = Image.new('RGB', (WIDTH, HEIGHT), BG_COLOR)
        draw = ImageDraw.Draw(img)

        header_text = f"{year}年{month}月 - {user_name}的鹿日历"
        draw.text((WIDTH / 2, 20), header_text, font=font_header, fill=HEADER_COLOR, anchor="mt")

        weekdays = ["一", "二", "三", "四", "五", "六", "日"]
        cell_width = WIDTH / 7
        for i, day in enumerate(weekdays):
            draw.text((i * cell_width + cell_width / 2, 90), day, font=font_weekday, fill=WEEKDAY_COLOR, anchor="mm")

        cal = calendar.monthcalendar(year, month)
        y_offset = 120
        cell_height = 75
        today_num = date.today().day if date.today().year == year and date.today().month == month else 0

        for week in cal:
            for i, day_num in enumerate(week):
                if day_num == 0:
                    continue
                x_pos = i * cell_width

                # 如果是今天，绘制一个淡蓝色背景
                if day_num == today_num:
                    draw.rectangle(
                        [x_pos, y_offset, x_pos + cell_width, y_offset + cell_height],
                        fill=TODAY_BG_COLOR
                    )

                # 绘制日期数字
                draw.text((x_pos + cell_width - 10, y_offset + 5), str(day_num), font=font_day, fill=DAY_COLOR,
                          anchor="ra")
                if day_num in checkin_data:
                    # 绘制 '√'
                    draw.text(
                        (x_pos + cell_width / 2, y_offset + cell_height / 2 - 5),
                        "√", font=font_check_mark, fill=CHECKIN_MARK_COLOR, anchor="mm"
                    )
                    # 绘制 '🦌'
                    deer_text = f"鹿了 {checkin_data[day_num]} 次"
                    draw.text(
                        (x_pos + cell_width / 2, y_offset + cell_height / 2 + 20),
                        deer_text, font=font_deer_count, fill=DEER_COUNT_COLOR, anchor="mm"
                    )
            y_offset += cell_height

        total_days = len(checkin_data)
        summary_text = f"本月总结：累计鹿了 {total_days} 天，共鹿 {total_deer} 次"
        draw.text((WIDTH / 2, HEIGHT - 30), summary_text, font=font_summary, fill=HEADER_COLOR, anchor="mm")

        file_path = os.path.join(self.temp_dir, f"checkin_{user_id}_{int(time.time())}.png")
        img.save(file_path, format='PNG')
        return file_path

    async def _generate_and_send_calendar(self, event: AstrMessageEvent):
        """查询和生成当月的打卡日历。"""
        user_id = event.get_sender_id()
        user_name = event.get_sender_name()
        current_year = date.today().year
        current_month = date.today().month
        current_month_str = date.today().strftime("%Y-%m")

        checkin_records = {}
        total_deer_this_month = 0
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                async with conn.execute(
                    "SELECT checkin_date, deer_count FROM checkin WHERE user_id = ? AND strftime('%Y-%m', checkin_date) = ?",
                    (user_id, current_month_str)
                ) as cursor:
                    rows = await cursor.fetchall()
                    if not rows:
                        yield event.plain_result("您本月还没有打卡记录哦，发送“🦌”开始第一次打卡吧！")
                        return

                    for row in rows:
                        day = int(row[0].split('-')[2])
                        count = row[1]
                        checkin_records[day] = count
                        total_deer_this_month += count
        except Exception as e:
            logger.error(f"查询用户 {user_name} ({user_id}) 的月度数据失败: {e}")
            yield event.plain_result("查询日历数据时出错了 >_<")
            return

        image_path = ""
        try:
            image_path = await asyncio.to_thread(
                self._create_calendar_image,
                user_id,
                user_name,
                current_year,
                current_month,
                checkin_records,
                total_deer_this_month
            )
            yield event.image_result(image_path)
        except FileNotFoundError:
            logger.error(f"字体文件未找到！无法生成日历图片。")
            yield event.plain_result(
                f"服务器缺少字体文件，无法生成日历图片。本月您已打卡{len(checkin_records)}天，累计{total_deer_this_month}个🦌。")
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

    async def terminate(self):
        """插件卸载/停用时调用"""
        logger.info("鹿打卡插件已卸载。")