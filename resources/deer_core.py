"""
鹿打卡核心工具函数模块
包含图像生成、数据分析、数据库查询等核心功能
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


class DeerCore:
    """鹿打卡插件的核心工具类"""
    
    def __init__(self, font_path: str, db_path: str, temp_dir: str):
        self.font_path = font_path
        self.db_path = db_path
        self.temp_dir = temp_dir

    async def _get_group_members(self, event, group_id: str) -> list:
        """获取群成员列表"""
        try:
            if event.get_platform_name() == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                if isinstance(event, AiocqhttpMessageEvent):
                    client = event.bot
                    members_info = await client.api.call_action('get_group_member_list', group_id=int(group_id))
                    return members_info if members_info else []
            return []
        except Exception as e:
            logger.error(f"获取群成员列表失败: {e}")
            return []

    async def _get_user_name(self, event, user_id: str) -> str:
        """获取用户昵称"""
        # 从 AstrMessageEvent 获取用户昵称
        try:
            # 如果是cqhttp平台
            if event.get_platform_name() == "aiocqhttp":
                from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent
                if isinstance(event, AiocqhttpMessageEvent):
                    group_id = event.get_group_id()
                    if group_id:
                        member_info = await event.bot.get_group_member_info(
                            group_id=int(group_id), user_id=int(user_id)
                        )
                        nickname = member_info.get("card") or member_info.get("nickname")
                        return nickname.strip() or str(user_id)
                    else:
                        stranger_info = await event.bot.get_stranger_info(user_id=int(user_id))
                        return stranger_info.get("nickname") or str(user_id)
            return str(user_id)
        except Exception:
            return str(user_id)

    def _create_ranking_image(self, user_names: list, ranking_data: list, year: int, month: int) -> str:
        """
        绘制月度打卡排行榜图片，参考日历图片风格
        """
        WIDTH = 700
        # 根据排行榜项目数量动态计算高度，确保所有项目都能显示
        ITEM_HEIGHT = 60
        HEADER_HEIGHT = 100
        FOOTER_HEIGHT = 60
        total_items = len(ranking_data)
        HEIGHT = max(600, HEADER_HEIGHT + ITEM_HEIGHT * total_items + FOOTER_HEIGHT)  # 最小高度600px

        BG_COLOR = (255, 255, 255)
        HEADER_COLOR = (50, 50, 50)
        WEEKDAY_COLOR = (100, 100, 100)
        DAY_COLOR = (80, 80, 80)
        DEER_COUNT_COLOR = (139, 69, 19)
        RANK_COLOR = (0, 150, 50)

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

        header_text = f"{year}年{month}月 - 鹿打卡排行榜"
        draw.text((WIDTH / 2, 20), header_text, font=font_header, fill=HEADER_COLOR, anchor="mt")

        y_offset = 100  # 从100px开始绘制项目
        item_height = ITEM_HEIGHT

        # 绘制排行榜项目
        for i, ((user_id, deer_count), user_name) in enumerate(zip(ranking_data, user_names)):
            # 绘制排名
            if i == 0:  # 冠军
                rank_text = "1.冠军"
                rank_color = (255, 215, 0)  # 金色
            elif i == 1:  # 亚军
                rank_text = "2.亚军"
                rank_color = (169, 169, 169)  # 银色
            elif i == 2:  # 季军
                rank_text = "3.季军"
                rank_color = (139, 69, 19)   # 铜色
            else:  # 其他
                rank_text = f"{i+1}."
                rank_color = RANK_COLOR      # 统一颜色

            # 绘制排名
            draw.text((50, y_offset + item_height / 2), rank_text, font=font_day, fill=rank_color, anchor="lm")

            # 绘制用户名
            draw.text((150, y_offset + item_height / 2), user_name, font=font_day, fill=DAY_COLOR, anchor="lm")

            # 绘制打卡次数
            deer_text = f"鹿 {deer_count} 次"
            draw.text((WIDTH - 50, y_offset + item_height / 2), deer_text, font=font_deer_count, fill=DEER_COUNT_COLOR, anchor="rm")

            y_offset += item_height

        # 添加底部总结
        total_displayed_users = len(ranking_data)
        summary_text = f"本群共有 {total_displayed_users} 人参与打卡"
        draw.text((WIDTH / 2, HEIGHT - 30), summary_text, font=font_summary, fill=HEADER_COLOR, anchor="mm")

        file_path = os.path.join(self.temp_dir, f"ranking_{year}_{month}_{int(time.time())}.png")
        img.save(file_path, format='PNG')
        return file_path

    async def _get_user_period_data(self, user_id: str, year: int, month: int) -> dict:
        """获取用户指定月份的打卡数据"""
        period_data = {}
        target_month_str = f"{year}-{month:02d}"

        try:
            async with aiosqlite.connect(self.db_path) as conn:
                async with conn.execute(
                    "SELECT checkin_date, deer_count FROM checkin WHERE user_id = ? AND strftime('%Y-%m', checkin_date) = ?",
                    (user_id, target_month_str)
                ) as cursor:
                    rows = await cursor.fetchall()
                    for row in rows:
                        date_str = row[0]
                        count = row[1]
                        day = int(date_str.split('-')[2])
                        period_data[day] = count
        except Exception as e:
            logger.error(f"查询用户 {user_id} 的 {year}年{month}月数据失败: {e}")
            return {}

        return period_data

    async def _get_user_yearly_data(self, user_id: str, year: int) -> dict:
        """获取用户指定年份的打卡数据"""
        yearly_data = {}
        try:
            async with aiosqlite.connect(self.db_path) as conn:
                async with conn.execute(
                    "SELECT checkin_date, deer_count FROM checkin WHERE user_id = ? AND strftime('%Y', checkin_date) = ?",
                    (user_id, str(year))
                ) as cursor:
                    rows = await cursor.fetchall()
                    for row in rows:
                        date_str = row[0]
                        count = row[1]
                        _, month, day = date_str.split('-')
                        month = int(month)
                        day = int(day)

                        if month not in yearly_data:
                            yearly_data[month] = {}
                        yearly_data[month][day] = count
        except Exception as e:
            logger.error(f"查询用户 {user_id} 的 {year}年数据失败: {e}")
            return {}

        return yearly_data

    async def _generate_monthly_analysis_report(self, user_name: str, year: int, month: int, period_data: dict) -> tuple[str, float]:
        """生成月度趣味打卡分析报告"""
        if not period_data:
            return "", 0.0

        from datetime import date
        import calendar

        total_days = len(period_data)          # 有记录的天数
        total_deer = sum(period_data.values()) # 总次数

        # 单日最高
        max_day_num, max_day_count = max(period_data.items(), key=lambda x: x[1])

        # 最长连续天数
        sorted_days = sorted(period_data.keys())
        max_consecutive = 1
        current = 1
        for i in range(1, len(sorted_days)):
            if sorted_days[i] == sorted_days[i-1] + 1:
                current += 1
                max_consecutive = max(max_consecutive, current)
            else:
                current = 1

        # 本月应分析天数 & 发射率
        days_in_month = calendar.monthrange(year, month)[1]
        today = date.today()
        analysis_days = today.day if year == today.year and month == today.month else days_in_month
        checkin_rate = total_days / analysis_days if analysis_days > 0 else 0
        freq_per_day = total_deer / analysis_days if analysis_days > 0 else 0

        # 纯文字幽默报告 - 仅以统计数据开始，无需标题（已在图片中）
        report = f"本月你一共动手 {total_days} 天，总计发射 {total_deer} 次。\n"

        if max_day_count == 1:
            report += f"每日节奏：温柔单发，优雅从容。\n"  # 或直接跳过
        else:
            if max_day_count >= 3:
                report += f"单日巅峰：{max_day_num}日 当天狂飙 {max_day_count} 次，手速已达职业级别，建议报名电竞。\n"
            elif max_day_count == 2:
                report += f"单日巅峰：{max_day_num}日 双杀达成，效率不错。\n"

        if max_consecutive >= 7:
            report += f"最长连击：连续 {max_consecutive} 天不带停！肾工厂已进入三班倒模式，建议立刻补货六味地黄丸。\n"
        elif max_consecutive >= 4:
            report += f"最长连击：连续 {max_consecutive} 天，节奏稳健，但腰子已经在悄悄报警了。\n"
        elif max_consecutive >= 2:
            report += f"最长连击：连续 {max_consecutive} 天，小连胜值得表扬。\n"

        report += f"本月发射率：{checkin_rate:.1%}\n\n"

        # 分级调侃
        if freq_per_day >= 1.3:
            report += "红色预警：重度沉迷选手！\n频率已突破安全线，肾上腺素秘书已向你腰子递交辞职信。\n再不控制，下个月可能要靠意念站立了。\n建议：多喝热水，多跑步，找点正经事干。"
        elif freq_per_day >= 0.7:
            report += "橙色警报：资深爱好者！\n手速稳定，但也该让右手放个假了。\n腰酸背痛没？下个月试试降到五成，奖励自己一顿烧烤？"
        elif freq_per_day >= 0.4:
            report += "黄色正常：中等频率，怡情有度。\n技术成熟，节奏掌握得当，继续保持即可。\n不过别忘了，现实中的桃花不会自己出现。"
        elif freq_per_day >= 0.1:
            report += "绿色健康：轻度选手！\n很有节制，肾在暗中给你点赞。\n继续努力，下个月争取再降一档，解锁自律达人称号。"
        else:
            report += "蓝色大师：几乎纯洁如白纸！\n本月肾气充盈，洪荒之力蓄势待发。\n小心哪天突然爆发，把床板震坏。\n坚持就是胜利！"

        report += "\n\n小贴士：适度怡情，过度伤身。\n健康第一，兄弟冲吧！"

        return report, checkin_rate

    async def _generate_yearly_analysis_report(self, user_name: str, year: int, yearly_data: dict) -> str:
        """生成年度趣味打卡分析报告（无emoji版）"""
        if not yearly_data:
            return ""

        total_months = len(yearly_data)
        total_days = sum(len(days) for days in yearly_data.values())
        total_deer = sum(sum(days.values()) for days in yearly_data.values())

        # 最活跃月份
        max_month = max(yearly_data.items(), key=lambda x: sum(x[1].values()))
        max_month_num, max_data = max_month
        max_month_deer = sum(max_data.values())

        report = f"全年共打卡 {total_months} 个月，{total_days} 天，总次数 {total_deer} 次。\n"
        report += f"最猛月份：{max_month_num}月，当月打卡 {max_month_deer} 次，那个月你到底经历了什么？\n\n"

        avg_per_month = total_deer / 12
        if avg_per_month > 25:
            report += "年度评价：核动力手指！\n全年无休，打卡工厂24小时加班生产。\n建议申报吉尼斯最强耐力纪录。"
        elif avg_per_month > 15:
            report += "年度评价：老司机稳如老狗！\n输出稳定，技术娴熟。\n明年可以尝试半戒模式，挑战更高难度。"
        elif avg_per_month > 8:
            report += "年度评价：中等玩家！\n有节制有放纵，生活平衡得不错，继续保持。"
        else:
            report += "年度评价：自律之王！\n基本纯洁，偶尔失守。\n手腕健康，明年继续当清心寡欲的典范。"

        report += "\n\n新的一年，愿你手指健康，生活充实。"

        return report

    def _create_analysis_image(self, user_name: str, target_period: str, analysis_result: str, checkin_rate: float = 0.0) -> str:
        """
        绘制分析报告图片
        """
        WIDTH, HEIGHT = 750, 550  # 稍微加宽加高，内容更舒展

        # 根据频率高低调整配色
        if checkin_rate >= 0.7:
            BG_COLOR = (255, 240, 240)     # 浅红背景
            HEADER_COLOR = (180, 0, 0)
        elif checkin_rate >= 0.4:
            BG_COLOR = (255, 250, 230)
            HEADER_COLOR = (160, 82, 45)
        else:
            BG_COLOR = (230, 245, 255)     # 浅蓝背景
            HEADER_COLOR = (0, 100, 160)

        TEXT_COLOR = (50, 50, 50)

        try:
            font_header = ImageFont.truetype(self.font_path, 32)
            font_content = ImageFont.truetype(self.font_path, 22)  # 字体大一点，更清晰
        except FileNotFoundError as e:
            logger.error(f"字体文件加载失败: {e}")
            raise e

        img = Image.new('RGB', (WIDTH, HEIGHT), BG_COLOR)
        draw = ImageDraw.Draw(img)

        # 绘制标题（居中）
        header_text = f"{target_period} {user_name}的鹿报告"
        header_bbox = draw.textbbox((0, 0), header_text, font=font_header)
        header_width = header_bbox[2] - header_bbox[0]
        draw.text(((WIDTH - header_width) // 2, 40), header_text, font=font_header, fill=HEADER_COLOR)

        # 分割报告为行，并处理空行
        lines = analysis_result.split('\n')
        y_offset = 100
        line_height = 35  # 关键：行高足够！（22号字 + 间距）

        for line in lines:
            line = line.strip()
            if not line:  # 空行
                y_offset += line_height // 2  # 空行只加一半高度
                continue

            # 计算文字宽度，实现居中（可选左对齐）
            bbox = draw.textbbox((0, 0), line, font=font_content)
            text_width = bbox[2] - bbox[0]
            x_pos = (WIDTH - text_width) // 2  # 居中显示
            # x_pos = 60  # 如果想左对齐，改成这个

            draw.text((x_pos, y_offset), line, font=font_content, fill=TEXT_COLOR)
            y_offset += line_height

        # 保存
        safe_period = target_period.replace('年', '_').replace('月', '')
        file_path = os.path.join(self.temp_dir, f"analysis_{user_name}_{safe_period}_{int(time.time())}.png")
        img.save(file_path, format='PNG')
        return file_path

    def _wrap_text(self, text: str, font, max_width: int) -> list:
        """
        文本自动换行
        """
        lines = []
        current_line = ""

        for char in text:
            test_line = current_line + char
            if font.getbbox(test_line)[2] <= max_width:
                current_line = test_line
            else:
                if current_line:
                    lines.append(current_line)
                current_line = char

        if current_line:
            lines.append(current_line)

        return lines

    def _create_yearly_calendar_image(self, user_id: str, user_name: str, year: int, yearly_data: dict) -> str:
        """
        绘制年度打卡日历图片，将12个月的日历按网格排列
        """
        from datetime import date
        import calendar

        # 显示从1月到当前月份（未来月份不显示）
        from datetime import datetime
        current_date = datetime.now()
        if year != current_date.year:
            months_to_show = 12
        else:
            months_to_show = current_date.month

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

        BG_COLOR = (255, 255, 255)
        HEADER_COLOR = (50, 50, 50)
        WEEKDAY_COLOR = (100, 100, 100)
        DAY_COLOR = (80, 80, 80)
        TODAY_BG_COLOR = (240, 240, 255)
        CHECKIN_MARK_COLOR = (0, 150, 50)
        DEER_COUNT_COLOR = (139, 69, 19)

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
        header_text = f"{year}年 - {user_name}的鹿年历"
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

                    # 如果是今天，绘制淡蓝色背景
                    if day_num == today_num and month == current_date.month:
                        draw.rectangle(
                            [day_x, day_y - 8, day_x + day_width, day_y + 8],
                            fill=TODAY_BG_COLOR
                        )

                    # 检查是否有打卡记录
                    if month in yearly_data and day_num in yearly_data[month]:
                        deer_count = yearly_data[month][day_num]
                        # 有打卡的日期使用红色
                        day_color = (255, 0, 0)  # 红色
                        # 绘制 '鹿' 数量
                        deer_text = f"{deer_count}"
                        draw.text(
                            (day_x + day_width / 2, day_y + 8),
                            deer_text, font=font_deer_count, fill=DEER_COUNT_COLOR, anchor="mm"
                        )
                    else:
                        # 没有打卡的日期使用普通颜色
                        day_color = DAY_COLOR

                    # 绘制日期数字
                    draw.text((day_x + day_width / 2, day_y), str(day_num), font=font_day, fill=day_color, anchor="mm")

        # 添加底部总结
        total_months = len(yearly_data)
        total_days = sum(len(days) for days in yearly_data.values())
        total_deer = sum(sum(days.values()) for days in yearly_data.values())

        summary_prefix = "本年总结" if year == datetime.now().year else "年度总结"
        summary_text = f"{summary_prefix}：{year}年累计打卡{total_months}个月，{total_days}天，共{total_deer}次"
        draw.text((img_width / 2, img_height - 20), summary_text, font=font_summary, fill=HEADER_COLOR, anchor="mm")

        file_path = os.path.join(self.temp_dir, f"yearly_calendar_{user_id}_{int(time.time())}.png")
        img.save(file_path, format='PNG')
        return file_path

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

    def _create_career_image(self, user_name: str, stats: dict) -> str:
        """
        绘制生涯报告图片
        stats 包含: first_date_str, total_span_days, total_count, total_days, daily_avg,
                    active_ratio, max_day_date, max_day_count, max_month_str, max_month_count,
                    min_month_str, min_month_count, rest_period_str, sage_comment,
                    status_day, status_comment, summary_comment
        """
        WIDTH = 800
        # 计算高度：头部 + 5个板块 + 底部留白
        # 板块内容增加，适当增高
        HEIGHT = 1100
        
        BG_COLOR = (255, 255, 255)
        TITLE_COLOR = (50, 50, 50)
        SUBTITLE_COLOR = (100, 100, 100)
        TEXT_COLOR = (80, 80, 80)
        HIGHLIGHT_COLOR = (139, 69, 19)  # 鹿的颜色
        SECTION_BG_COLOR = (248, 248, 255)
        COMMENT_COLOR = (120, 120, 120) # 评语颜色
        
        img = Image.new('RGB', (WIDTH, HEIGHT), BG_COLOR)
        draw = ImageDraw.Draw(img)
        
        try:
            font_title = ImageFont.truetype(self.font_path, 40)
            font_subtitle = ImageFont.truetype(self.font_path, 24)
            font_section_title = ImageFont.truetype(self.font_path, 28)
            font_text = ImageFont.truetype(self.font_path, 24)
            font_small = ImageFont.truetype(self.font_path, 20)
            font_comment = ImageFont.truetype(self.font_path, 20) # 评语字体
        except Exception:
            # Fallback if font fails
            font_title = ImageFont.load_default()
            font_subtitle = ImageFont.load_default()
            font_section_title = ImageFont.load_default()
            font_text = ImageFont.load_default()
            font_small = ImageFont.load_default()
            font_comment = ImageFont.load_default()

        # 1. 标题区域
        y_pos = 50
        draw.text((WIDTH / 2, y_pos), "鹿生涯档案", font=font_title, fill=TITLE_COLOR, anchor="mm")
        y_pos += 50
        draw.text((WIDTH / 2, y_pos), f"选手：{user_name}", font=font_subtitle, fill=SUBTITLE_COLOR, anchor="mm")
        
        # 顶部总评
        y_pos += 40
        draw.text((WIDTH / 2, y_pos), f"“{stats['summary_comment']}”", font=font_section_title, fill=HIGHLIGHT_COLOR, anchor="mm")
        
        y_pos += 60

        # 定义板块绘制函数
        def draw_section(title, lines, start_y):
            # 预计算行高
            content_height = 0
            processed_lines = [] # list of (text, font, color)
            
            for item in lines:
                text = item['text']
                is_comment = item.get('is_comment', False)
                font = font_comment if is_comment else font_text
                color = COMMENT_COLOR if is_comment else TEXT_COLOR
                offset = 30 if is_comment else 35
                processed_lines.append((text, font, color, offset))
                content_height += offset
            
            # 绘制板块背景
            section_height = 40 + content_height + 20
            draw.rectangle(
                [40, start_y, WIDTH - 40, start_y + section_height],
                fill=SECTION_BG_COLOR,
                outline=(230, 230, 230),
                width=1
            )
            
            # 绘制板块标题
            current_y = start_y + 25
            draw.text((60, current_y), title, font=font_section_title, fill=HIGHLIGHT_COLOR, anchor="lm")
            
            # 绘制内容
            current_y += 40
            for text, font, color, offset in processed_lines:
                draw.text((80, current_y), text, font=font, fill=color, anchor="lm")
                current_y += offset
            
            return start_y + section_height + 30

        # 2. 生涯起点
        lines = [
            {'text': f"{stats['first_date_str']} (距今 {stats['total_span_days']} 天)"}
        ]
        y_pos = draw_section("生涯起点", lines, y_pos)

        # 3. 累计战绩
        avg_display = ""
        if stats['daily_avg'] > 1:
            avg_display = f"日均发射：{stats['daily_avg']:.2f} 次"
        elif stats['daily_avg'] > 0:
            interval = 1 / stats['daily_avg']
            avg_display = f"平均频率：每 {interval:.1f} 天 1 次"
        else:
            avg_display = "日均发射：0 次"

        lines = [
            {'text': f"总计发射：{stats['total_count']} 次"},
            {'text': f"动手天数：{stats['total_days']} 天 (占比 {stats['active_ratio']:.1f}%)"},
            {'text': avg_display}
        ]
        y_pos = draw_section("累计战绩", lines, y_pos)

        # 4. 巅峰时刻
        lines = []
        if stats['max_day_count'] > 1:
            lines.append({'text': f"单日之最：{stats['max_day_date']} ({stats['max_day_count']} 次)"})
        
        if stats['max_month_count'] > 0:
            lines.append({'text': f"月度之最：{stats['max_month_str']} ({stats['max_month_count']} 次)"})
             
        y_pos = draw_section("巅峰时刻", lines, y_pos)

        # 5. 贤者时期
        lines = [
            {'text': f"最少月份：{stats['min_month_str']} ({stats['min_month_count']} 次)"},
            {'text': f"最长休养：{stats['rest_period_str']}"}
        ]
        if stats['sage_comment']:
             lines.append({'text': f"({stats['sage_comment']})", 'is_comment': True})
             
        y_pos = draw_section("贤者时期", lines, y_pos)

        # 6. 当前状态
        lines = [
            {'text': f"距离上次：Day {stats['status_day']}"}
        ]
        if stats['status_comment']:
            lines.append({'text': f"({stats['status_comment']})", 'is_comment': True})
            
        y_pos = draw_section("当前状态", lines, y_pos)

        # 保存图片
        file_path = os.path.join(self.temp_dir, f"career_{int(time.time())}.png")
        img.save(file_path)
        return file_path

    async def _generate_and_send_calendar(self, event, user_id: str, user_name: str, db_path: str):
        """查询和生成当月的打卡日历。"""
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
                        return "您本月还没有打卡记录哦，发送“🦌”开始第一次打卡吧！", None, False

                    for row in rows:
                        day = int(row[0].split('-')[2])
                        count = row[1]
                        checkin_records[day] = count
                        total_deer_this_month += count
        except Exception as e:
            logger.error(f"查询用户 {user_name} ({user_id}) 的月度数据失败: {e}")
            return "查询日历数据时出错了 >_<", None, True

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
            return None, image_path, False
        except FileNotFoundError:
            logger.error(f"字体文件未找到！无法生成日历图片。")
            return (
                f"服务器缺少字体文件，无法生成日历图片。本月您已打卡{len(checkin_records)}天，累计{total_deer_this_month}个🦌。",
                None,
                False
            )
        except Exception as e:
            logger.error(f"生成或发送日历图片失败: {e}")
            return "处理日历图片时发生了未知错误 >_<", None, True