from datetime import date, datetime, timedelta, timezone

CN_TZ = timezone(timedelta(hours=8))


def today_cn() -> date:
    """按中国日期判断证件是否过期（截止日当天仍有效）。"""
    return datetime.now(CN_TZ).date()


def classify(ch4_pct: float) -> tuple[str, str]:
    if ch4_pct >= 1.0:
        return "报警", "甲烷达到报警线"
    return "正常", "甲烷低于报警线"
