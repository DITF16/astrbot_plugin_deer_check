"""
Klittra功能模块
提供与 deer 日历类似的功能，但用于扣日历记录
"""

import aiosqlite
import calendar
from datetime import date, datetime, timedelta
from PIL import Image, ImageDraw, ImageFont
import os
import re
import time
import asyncio
from astrbot.api import logger


class KlittraCore:
    """Klittra核心工具类"""

    def __init__(self, font_path: str, db_path: str, temp_dir: str):
        self.font_path = font_path
        self.db_path = db_path
        self.temp_dir = temp_dir

    def _create_klittra_calendar_image(self, user_id: str, user_name: str, year: int, month: int, checkin_data: dict, total_deer: int) -> str:
        """
        绘制用户月度扣日历图片
        """
        WIDTH, HEIGHT = 700, 620
        BG_COLOR = (255, 240, 245)  # 淡粉色背景
        HEADER_COLOR = (180, 30, 60)  # 粉红色标题
        WEEKDAY_COLOR = (150, 70, 100)  # 深粉色星期标题
        DAY_COLOR = (100, 50, 80)  # 深粉色日期
        TODAY_BG_COLOR = (255, 220, 230)  # 淡粉色今天背景
        CHECKIN_MARK_COLOR = (255, 100, 150)  # 粉红色打卡标记
        DEER_COUNT_COLOR = (200, 50, 100)  # 粉红色计数

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

        header_text = f"{year}年{month}月 - {user_name}的扣日历"
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

                # 如果是今天，绘制一个淡粉色背景
                if day_num == today_num:
                    draw.rectangle(
                        [x_pos, y_offset, x_pos + cell_width, y_offset + cell_height],
                        fill=TODAY_BG_COLOR
                    )

                # 绘制日期数字
                draw.text((x_pos + cell_width - 10, y_offset + 5), str(day_num), font=font_day, fill=DAY_COLOR,
                          anchor="ra")
                if day_num in checkin_data:
                    # 绘制 '✓'
                    draw.text(
                        (x_pos + cell_width / 2, y_offset + cell_height / 2 - 5),
                        "✓", font=font_check_mark, fill=CHECKIN_MARK_COLOR, anchor="mm"
                    )
                    # 绘制 '周期'
                    cycle_text = f"扣了 {checkin_data[day_num]} 次"
                    draw.text(
                        (x_pos + cell_width / 2, y_offset + cell_height / 2 + 20),
                        cycle_text, font=font_deer_count, fill=DEER_COUNT_COLOR, anchor="mm"
                    )
            y_offset += cell_height

        total_days = len(checkin_data)
        summary_text = f"本月总结：累计扣了 {total_days} 天，共 {total_deer} 次"
        draw.text((WIDTH / 2, HEIGHT - 30), summary_text, font=font_summary, fill=HEADER_COLOR, anchor="mm")

        file_path = os.path.join(self.temp_dir, f"klittra_calendar_{user_id}_{int(time.time())}.png")
        img.save(file_path, format='PNG')
        return file_path

    def _create_klittra_yearly_calendar_image(self, user_id: str, user_name: str, year: int, yearly_data: dict) -> str:
        """
        绘制扣年度日历图片，将12个月的日历按网格排列
        """
        from datetime import date
        import calendar

        # 显示从1月到当前月份（未来月份不显示）
        from datetime import datetime
        current_month = datetime.now().month
        months_to_show = current_month  # 显示1月到当前月份

        # 定义每行显示的月份数量
        months_per_row = 3
        rows_needed = (months_to_show + months_per_row - 1) // months_per_row  # 向上取整

        # 定义单个月历的尺寸
        single_cal_width = 200
        single_cal_height = 180
        header_height = 30
        margin = 20

        # 计算整体图片尺寸
        img_width = months_per_row * single_cal_width + (months_per_row + 1) * margin
        img_height = rows_needed * single_cal_height + (rows_needed + 1) * margin + 50  # 额外空间用于标题

        BG_COLOR = (255, 240, 245)  # 淡粉色背景
        HEADER_COLOR = (180, 30, 60)  # 粉红色标题
        WEEKDAY_COLOR = (150, 70, 100)  # 深粉色星期标题
        DAY_COLOR = (100, 50, 80)  # 深粉色日期
        TODAY_BG_COLOR = (255, 220, 230)  # 淡粉色今天背景
        CHECKIN_MARK_COLOR = (255, 100, 150)  # 粉红色打卡标记
        DEER_COUNT_COLOR = (200, 50, 100)  # 粉红色计数

        try:
            font_header = ImageFont.truetype(self.font_path, 24)
            font_weekday = ImageFont.truetype(self.font_path, 10)
            font_day = ImageFont.truetype(self.font_path, 12)
            font_check_mark = ImageFont.truetype(self.font_path, 14)
            font_deer_count = ImageFont.truetype(self.font_path, 8)
            font_summary = ImageFont.truetype(self.font_path, 18)
        except FileNotFoundError as e:
            logger.error(f"字体文件加载失败: {e}")
            raise e

        img = Image.new('RGB', (img_width, img_height), BG_COLOR)
        draw = ImageDraw.Draw(img)

        # 绘制标题
        header_text = f"{year}年 - {user_name}的扣年历"
        draw.text((img_width / 2, 20), header_text, font=font_header, fill=HEADER_COLOR, anchor="mt")

        # 绘制每个月的日历
        for i, month in enumerate(range(1, months_to_show + 1)):
            row = i // months_per_row
            col = i % months_per_row

            # 计算这个月历的左上角坐标
            x_offset = margin + col * (single_cal_width + margin)
            y_offset = 50 + margin + row * (single_cal_height + margin)

            # 绘制月份标题
            month_text = f"{month}月"
            draw.text((x_offset + single_cal_width / 2, y_offset), month_text, font=font_weekday, fill=HEADER_COLOR, anchor="mt")

            # 绘制星期标题
            weekdays = ["一", "二", "三", "四", "五", "六", "日"]
            day_width = single_cal_width // 7
            for j, day in enumerate(weekdays):
                draw.text(
                    (x_offset + j * day_width + day_width / 2, y_offset + header_height),
                    day,
                    font=font_weekday,
                    fill=WEEKDAY_COLOR,
                    anchor="mm"
                )

            # 绘制日期
            cal = calendar.monthcalendar(year, month)
            current_date = date.today()
            today_num = current_date.day if current_date.year == year and current_date.month == month else 0

            for week_idx, week in enumerate(cal):
                for day_idx, day_num in enumerate(week):
                    if day_num == 0:  # 0表示不属于当前月的日期
                        continue

                    day_x = x_offset + day_idx * day_width
                    day_y = y_offset + header_height + 15 + week_idx * 20  # 15是星期标题高度，20是行间距

                    # 如果是今天，绘制淡粉色背景
                    if day_num == today_num and month == current_date.month:
                        draw.rectangle(
                            [day_x, day_y - 8, day_x + day_width, day_y + 8],
                            fill=TODAY_BG_COLOR
                        )

                    # 检查是否有记录
                    if month in yearly_data and day_num in yearly_data[month]:
                        cycle_count = yearly_data[month][day_num]
                        # 有记录的日期使用红色
                        day_color = (255, 0, 0)  # 红色
                        # 绘制 '周期' 数量
                        cycle_text = f"{cycle_count}"
                        draw.text(
                            (day_x + day_width / 2, day_y + 8),
                            cycle_text, font=font_deer_count, fill=DEER_COUNT_COLOR, anchor="mm"
                        )
                    else:
                        # 没有记录的日期使用普通颜色
                        day_color = DAY_COLOR

                    # 绘制日期数字
                    draw.text((day_x + day_width / 2, day_y), str(day_num), font=font_day, fill=day_color, anchor="mm")

        # 添加底部总结
        total_months = len(yearly_data)
        total_days = sum(len(days) for days in yearly_data.values())
        total_deer = sum(sum(days.values()) for days in yearly_data.values())
        summary_text = f"年度总结：{year}年累计扣了{total_months}个月，{total_days}天，共{total_deer}次"
        draw.text((img_width / 2, img_height - 20), summary_text, font=font_summary, fill=HEADER_COLOR, anchor="mm")

        file_path = os.path.join(self.temp_dir, f"klittra_yearly_calendar_{user_id}_{int(time.time())}.png")
        img.save(file_path, format='PNG')
        return file_path

    async def _generate_and_send_klittra_calendar(self, event, user_id: str, user_name: str, db_path: str):
        """查询和生成当月的扣日历。"""
        current_year = date.today().year
        current_month = date.today().month
        current_month_str = date.today().strftime("%Y-%m")

        checkin_records = {}
        total_deer_this_month = 0
        try:
            async with aiosqlite.connect(db_path) as conn:
                async with conn.execute(
                    "SELECT checkin_date, deer_count FROM checkin WHERE user_id = ? AND strftime('%Y-%m', checkin_date) = ?",
                    (user_id, current_month_str)
                ) as cursor:
                    rows = await cursor.fetchall()
                    if not rows:
                        return "您本月还没有扣日历记录哦，发送“🤏”开始第一次记录吧！", None, False

                    for row in rows:
                        day = int(row[0].split('-')[2])
                        count = row[1]
                        checkin_records[day] = count
                        total_deer_this_month += count
        except Exception as e:
            logger.error(f"查询用户 {user_name} ({user_id}) 的扣月度数据失败: {e}")
            return "查询扣日历数据时出错了 >_<", None, True

        image_path = ""
        try:
            image_path = await asyncio.to_thread(
                self._create_klittra_calendar_image,
                user_id,
                user_name,
                current_year,
                current_month,
                checkin_records,
                total_deer_this_month
            )
            return None, image_path, False
        except FileNotFoundError:
            logger.error(f"字体文件未找到！无法生成扣日历图片。")
            return (
                f"服务器缺少字体文件，无法生成扣日历图片。本月您已扣了{len(checkin_records)}天，累计{total_deer_this_month}次。",
                None,
                False
            )
        except Exception as e:
            logger.error(f"生成或发送扣日历图片失败: {e}")
            return "处理扣日历图片时发生了未知错误 >_<", None, True