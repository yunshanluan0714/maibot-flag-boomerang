"""Candidate prefilter, Chinese deadline parser, sleep window math, and LLM prompt/parsers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any
from zoneinfo import ZoneInfo

try:
    from .store import FlagRecord
except ImportError:
    from store import FlagRecord  # type: ignore[no-redef]

_MEDIA_TAIL_RE = re.compile(r"\[动图\][\s\S]*")
_SYSTEM_BRACKET_RE = re.compile(
    r"\[(?:表情包|图片|语音|视频|小程序|文件|音乐|分享|转发|合并转发|QQ红包|红包|事件-[^\]\n]*|网页链接摘要|B站视频信息)[^\]]*\]"
)
_AT_TAG_RE = re.compile(r"<at\b[^>]*>|@\S+")
_URL_RE = re.compile(r"https?://\S+", re.I)
_REPLY_PREFIX_RE = re.compile(r"^(?:回复了[^\n:：]{1,32}?的消息[:：]?\s*|\[引用[^\]]*\]\s*)+")
_SYSTEM_NOTICE_RE = re.compile(
    r"^(?:【[^】]{1,20}(?:提醒|通知|播报|报告|告警|警告|状态|结果|防撤回)】|\[系统通知\])"
)

_SLEEP_VOW_RE = re.compile(
    r"(?:今晚|今天|今夜|这周|以后).{0,12}(?:早睡|必睡|一定睡|准时睡|绝不熬夜|不熬夜了|睡觉了)|"
    r"(?:再熬夜|熬夜我是|熬夜就).{0,10}|"
    r"(?:(?:不超过|超过|到)?\s*(?:[01]?\d|2[0-3]|十一|十二|十|一|二|两|三)\s*(?:[:：]|点(?:半|[0-5]?\d分?)?)\s*(?:前|之前)?\s*(?:必睡|一定睡|睡觉|还不睡|不熬夜))"
)
_SLEEP_CUTOFF_RE = re.compile(
    r"(?:(?P<h>2[0-3]|1?\d|十一|十二|十|一|二|两|三))\s*(?:[:：]\s*(?P<m1>[0-5]\d)|点\s*(?P<half>半)?(?P<m2>[0-5]?\d)?分?)"
)

_SPEND_GACHA_RE = re.compile(
    r"(?:这(?:个|期)?(?:池子|up|版本|轮).{0,6}(?:绝?对?不抽|不碰|跳过|pass))|"
    r"(?:绝?对?不(?:再)?(?:抽卡?|氪金?|充钱?|买了?|剁手|喝奶茶|碰奶茶)|"
    r"再(?:抽|氪|充|买|下单|点外卖|喝奶茶).{0,6}(?:我是|我就是|我就|剁手|倒立)|"
    r"戒(?:奶茶|可乐|甜食|外卖|游戏|抽卡|氪金)|"
    r"倒立吃(?:键盘|显示器|手机))"
)

_DEADLINE_GOAL_RE = re.compile(
    r"(?:立(?:个|下)?\s*(?:flag|Flag|FLAG)|发誓|保底一定|一定(?:把|要|去|完成|考过|写完|减|瘦)|"
    r"(?:明天|后天|大后天|本周|这周|下周|月底|下个月|[一二两三四五六七八九十\d]+(?:小时|天|周|个月)(?:内|前)?).{0,10}(?:必|一定|绝不|搞定|交稿|上岸|做完|写完|瘦|减重|存够))"
)

_BOLD_PREDICTION_RE = re.compile(
    r"(?:立[帖贴]为证|(?:把)?话放这(?:里|儿)?|毒奶一口|必(?:暴死|凉|翻车|寄|出事)|绝对(?:凉|翻车|寄)|"
    r"赌(?:五毛|一块|十块|一顿|一杯|命)|(?:要是|如果)[^。！？\n]{2,20}(?:我就|我直播|我请|我发红包))"
)

_PURE_QUESTION_RE = re.compile(r"(?:[?？]|吗|呢|呀|吧)\s*$")
_STRONG_VOW_MARKER_RE = re.compile(r"必|一定|绝不|发誓|立[帖贴]为证|话放这|再.{1,4}(?:我是|我就|剁手)|flag", re.I)

_THIRD_PERSON_RE = re.compile(r"(?:我朋友|我室友|我同学|我哥|我弟|我姐|他|她|群友|别人|有人|大佬)")

_CN_NUM = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


@dataclass(frozen=True)
class CandidateSignal:
    category: str
    base_confidence: float
    rule_summary: str
    sleep_cutoff_hhmm: str
    deadline_ts: float | None
    bust_keywords: tuple[str, ...]


@dataclass(frozen=True)
class ParsedFlag:
    is_flag: bool
    category: str
    summary: str
    deadline_ts: float | None
    sleep_cutoff_ts: float | None
    sleep_window_end_ts: float | None
    bust_keywords: tuple[str, ...]
    confidence: float


def resolve_tz(tz_name: str) -> Any:
    name = str(tz_name or "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        return ZoneInfo(name)
    except Exception:
        return timezone(timedelta(hours=8))


def parse_hhmm(value: str, fallback: tuple[int, int]) -> int:
    text = str(value or "").strip()
    try:
        parts = text.split(":")
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h * 60 + m
    except Exception:
        pass
    return fallback[0] * 60 + fallback[1]


def in_quiet_hours(now_dt: datetime, quiet_start: str = "01:00", quiet_end: str = "08:00") -> bool:
    cur_min = now_dt.hour * 60 + now_dt.minute
    s_min = parse_hhmm(quiet_start, (1, 0))
    e_min = parse_hhmm(quiet_end, (8, 0))
    if s_min == e_min:
        return False
    if s_min < e_min:
        return s_min <= cur_min < e_min
    return cur_min >= s_min or cur_min < e_min


def _cn_to_int(token: str) -> int | None:
    raw = str(token or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return int(raw)
    if raw in _CN_NUM:
        return _CN_NUM[raw]
    if raw.startswith("十") and len(raw) == 2 and raw[1] in _CN_NUM:
        return 10 + _CN_NUM[raw[1]]
    if len(raw) == 2 and raw[1] == "十" and raw[0] in _CN_NUM:
        return _CN_NUM[raw[0]] * 10
    if len(raw) == 3 and raw[1] == "十" and raw[0] in _CN_NUM and raw[2] in _CN_NUM:
        return _CN_NUM[raw[0]] * 10 + _CN_NUM[raw[2]]
    return None


def sanitize_incoming_text(text: str) -> str:
    cleaned = _MEDIA_TAIL_RE.sub(" ", str(text or ""))
    cleaned = _SYSTEM_BRACKET_RE.sub(" ", cleaned)
    cleaned = _AT_TAG_RE.sub(" ", cleaned)
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = _REPLY_PREFIX_RE.sub("", cleaned.strip())
    return " ".join(cleaned.split()).strip()


def is_system_or_question(clean_text: str) -> bool:
    if not clean_text:
        return True
    if _SYSTEM_NOTICE_RE.match(clean_text):
        return True
    if _PURE_QUESTION_RE.search(clean_text) and not _STRONG_VOW_MARKER_RE.search(clean_text):
        return True
    return False


def _extract_sleep_cutoff_hhmm(text: str) -> str:
    m = _SLEEP_CUTOFF_RE.search(text)
    if not m:
        return "00:00"
    h_raw = m.group("h")
    hour = _cn_to_int(h_raw)
    if hour is None:
        return "00:00"
    # "11点" / "十一点" -> 23; "12点" / "十二点" -> 0; "1点".."3点" -> 1..3
    if hour in (10, 11):
        hour += 12
    elif hour == 12:
        hour = 0
    elif 4 <= hour <= 19:
        return "00:00"
    minute = 0
    if m.group("half"):
        minute = 30
    elif m.group("m1"):
        minute = int(m.group("m1"))
    elif m.group("m2"):
        minute = int(m.group("m2"))
    minute = max(0, min(59, minute))
    return f"{hour:02d}:{minute:02d}"


def compute_sleep_window(cutoff_hhmm: str, now_dt: datetime, *, grace_minutes: int = 15) -> tuple[float, float]:
    parts = cutoff_hhmm.split(":")
    ch = int(parts[0]) if parts[0].isdigit() else 0
    cm = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0

    cur_date = now_dt.date()
    if now_dt.hour >= 6:
        target_date = cur_date if ch >= 20 else cur_date + timedelta(days=1)
    else:
        if ch >= 20:
            target_date = cur_date
        else:
            cand = datetime(cur_date.year, cur_date.month, cur_date.day, ch, cm, tzinfo=now_dt.tzinfo)
            target_date = cur_date if cand > now_dt else cur_date + timedelta(days=1)

    cutoff_dt = datetime(target_date.year, target_date.month, target_date.day, ch, cm, tzinfo=now_dt.tzinfo)
    bust_start_dt = cutoff_dt + timedelta(minutes=grace_minutes)

    morning_date = cutoff_dt.date() + timedelta(days=1) if cutoff_dt.hour >= 20 else cutoff_dt.date()
    window_end_dt = datetime(morning_date.year, morning_date.month, morning_date.day, 5, 30, tzinfo=now_dt.tzinfo)
    return bust_start_dt.timestamp(), window_end_dt.timestamp()


def _end_of_day(dt: datetime, hour: int = 21, minute: int = 0) -> datetime:
    return datetime(dt.year, dt.month, dt.day, hour, minute, tzinfo=dt.tzinfo)


def _month_last_day(year: int, month: int) -> int:
    if month == 12:
        nxt = datetime(year + 1, 1, 1)
    else:
        nxt = datetime(year, month + 1, 1)
    return (nxt - timedelta(days=1)).day


def parse_chinese_deadline(text: str, now_dt: datetime) -> float | None:
    clean = str(text or "").strip()
    if not clean:
        return None

    m_hour = re.search(r"([一二两三四五六七八九十\d]{1,3})\s*(?:个)?小时(?:后|内)", clean)
    if m_hour:
        val = _cn_to_int(m_hour.group(1))
        if val and 1 <= val <= 168:
            return (now_dt + timedelta(hours=val)).timestamp()

    m_day = re.search(r"([一二两三四五六七八九十\d]{1,3})\s*天(?:后|内)", clean)
    if m_day:
        val = _cn_to_int(m_day.group(1))
        if val and 1 <= val <= 90:
            return _end_of_day(now_dt + timedelta(days=val)).timestamp()

    if "大后天" in clean:
        return _end_of_day(now_dt + timedelta(days=3)).timestamp()
    if "后天" in clean:
        return _end_of_day(now_dt + timedelta(days=2)).timestamp()
    if "明天" in clean or "明晚" in clean:
        h = 9 if "早" in clean else 21
        return _end_of_day(now_dt + timedelta(days=1), hour=h).timestamp()

    if "下个月" in clean or "下月" in clean:
        ny = now_dt.year + (1 if now_dt.month == 12 else 0)
        nm = 1 if now_dt.month == 12 else now_dt.month + 1
        last = _month_last_day(ny, nm)
        return datetime(ny, nm, last, 21, 0, tzinfo=now_dt.tzinfo).timestamp()

    if "月底" in clean or "本月底" in clean:
        last = _month_last_day(now_dt.year, now_dt.month)
        return datetime(now_dt.year, now_dt.month, last, 21, 0, tzinfo=now_dt.tzinfo).timestamp()

    if "本周末" in clean or "这周" in clean or "本周" in clean:
        days_to_sun = (6 - now_dt.weekday()) % 7
        target = now_dt + timedelta(days=days_to_sun or 7 if "本周" in clean and now_dt.weekday() == 6 else days_to_sun)
        return _end_of_day(target).timestamp()

    m_next_wk = re.search(r"下周([一二三四五六日天])", clean)
    if m_next_wk:
        wk_map = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
        target_wd = wk_map[m_next_wk.group(1)]
        days_ahead = (7 - now_dt.weekday()) + target_wd
        return _end_of_day(now_dt + timedelta(days=days_ahead)).timestamp()

    m_date = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", clean)
    if m_date:
        mo, da = int(m_date.group(1)), int(m_date.group(2))
        if 1 <= mo <= 12 and 1 <= da <= 31:
            yr = now_dt.year
            try:
                cand = datetime(yr, mo, da, 21, 0, tzinfo=now_dt.tzinfo)
                if cand < now_dt - timedelta(days=1):
                    cand = datetime(yr + 1, mo, da, 21, 0, tzinfo=now_dt.tzinfo)
                return cand.timestamp()
            except ValueError:
                pass

    return None


def _derive_rule_bust_keywords(text: str, category: str) -> tuple[str, ...]:
    kws: list[str] = []
    if category == "spend_gacha_vow":
        if any(k in text for k in ("抽", "池", "歪", "up", "UP")):
            kws.extend(["抽了", "真香", "歪了", "出金", "十连", "小保底", "大保底", "全图鉴"])
        if any(k in text for k in ("氪", "充", "买", "剁手")):
            kws.extend(["氪了", "充了", "真香", "648", "下单", "买了"])
        if "奶茶" in text:
            kws.extend(["奶茶", "真香", "喝了一杯", "点了一杯"])
        if "外卖" in text:
            kws.extend(["外卖", "点了", "真香"])
        if not kws:
            kws.extend(["真香", "破戒", "没忍住"])
    elif category == "bold_prediction":
        kws.extend(["真香", "打脸", "翻车", "奶中了", "寄了"])
    elif category == "deadline_goal":
        kws.extend(["鸽了", "没写完", "放弃", "摆烂"])
    return tuple(dict.fromkeys(kws))


def prefilter_flag_candidate(text: str, now_dt: datetime) -> CandidateSignal | None:
    clean = sanitize_incoming_text(text)
    if len(clean) < 5 or len(clean) > 180:
        return None
    if is_system_or_question(clean):
        return None

    if _SLEEP_VOW_RE.search(clean):
        cutoff = _extract_sleep_cutoff_hhmm(clean)
        return CandidateSignal(
            category="sleep_vow",
            base_confidence=0.86,
            rule_summary=clean[:80],
            sleep_cutoff_hhmm=cutoff,
            deadline_ts=None,
            bust_keywords=(),
        )

    if _SPEND_GACHA_RE.search(clean):
        return CandidateSignal(
            category="spend_gacha_vow",
            base_confidence=0.84,
            rule_summary=clean[:80],
            sleep_cutoff_hhmm="",
            deadline_ts=parse_chinese_deadline(clean, now_dt),
            bust_keywords=_derive_rule_bust_keywords(clean, "spend_gacha_vow"),
        )

    if _BOLD_PREDICTION_RE.search(clean):
        return CandidateSignal(
            category="bold_prediction",
            base_confidence=0.76,
            rule_summary=clean[:80],
            sleep_cutoff_hhmm="",
            deadline_ts=parse_chinese_deadline(clean, now_dt),
            bust_keywords=_derive_rule_bust_keywords(clean, "bold_prediction"),
        )

    if _DEADLINE_GOAL_RE.search(clean):
        dl = parse_chinese_deadline(clean, now_dt)
        return CandidateSignal(
            category="deadline_goal",
            base_confidence=0.78,
            rule_summary=clean[:80],
            sleep_cutoff_hhmm="",
            deadline_ts=dl,
            bust_keywords=_derive_rule_bust_keywords(clean, "deadline_goal"),
        )

    return None


def fallback_parse_candidate(candidate: CandidateSignal, now_dt: datetime) -> ParsedFlag:
    sleep_cutoff_ts: float | None = None
    sleep_window_end_ts: float | None = None
    if candidate.category == "sleep_vow":
        sleep_cutoff_ts, sleep_window_end_ts = compute_sleep_window(
            candidate.sleep_cutoff_hhmm or "00:00",
            now_dt,
        )
    return ParsedFlag(
        is_flag=True,
        category=candidate.category,
        summary=candidate.rule_summary,
        deadline_ts=candidate.deadline_ts,
        sleep_cutoff_ts=sleep_cutoff_ts,
        sleep_window_end_ts=sleep_window_end_ts,
        bust_keywords=candidate.bust_keywords,
        confidence=candidate.base_confidence,
    )


def _extract_json_block(raw_text: str) -> dict[str, Any] | None:
    text = str(raw_text or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.I)
    if fenced:
        text = fenced.group(1).strip()
    else:
        brace = re.search(r"\{[\s\S]*\}", text)
        if brace:
            text = brace.group(0)
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def build_extract_prompt(user_name: str, text: str, now_dt: datetime) -> str:
    now_str = now_dt.strftime("%Y-%m-%d %H:%M (%A)")
    return (
        "你是群聊 Flag 识别器。判断下面群友发言是不是在“认真/口嗨立Flag、立早睡誓言、立戒氪/戒断毒誓、或放狠话毒奶”。\n"
        "普通提问、日常抱怨、转述别人、开玩笑反问均不算。\n"
        f"当前时间：{now_str}\n"
        f"发言人：{user_name}\n"
        f"原话：{text}\n\n"
        "仅输出一行合法 JSON（不要解释），格式：\n"
        '{"is_flag": true/false, "category": "sleep_vow|spend_gacha_vow|deadline_goal|bold_prediction", '
        '"summary": "不超过24字的凝练概括", "sleep_cutoff_hhmm": "23:00或空", '
        '"deadline_iso": "YYYY-MM-DD HH:MM或空", "bust_keywords": ["反面打脸词1", "反面打脸词2"], "confidence": 0.0}'
    )


def parse_llm_extract_response(
    raw_text: str,
    candidate: CandidateSignal,
    now_dt: datetime,
) -> ParsedFlag | None:
    data = _extract_json_block(raw_text)
    if data is None:
        return None

    is_flag = bool(data.get("is_flag", False))
    try:
        conf = float(data.get("confidence", candidate.base_confidence))
    except (TypeError, ValueError):
        conf = candidate.base_confidence
    conf = max(0.0, min(1.0, conf))

    if not is_flag:
        return ParsedFlag(
            is_flag=False,
            category=candidate.category,
            summary="",
            deadline_ts=None,
            sleep_cutoff_ts=None,
            sleep_window_end_ts=None,
            bust_keywords=(),
            confidence=conf,
        )

    cat = str(data.get("category") or candidate.category).strip()
    if cat not in ("sleep_vow", "spend_gacha_vow", "deadline_goal", "bold_prediction"):
        cat = candidate.category

    summary = str(data.get("summary") or candidate.rule_summary).strip()[:80]
    cutoff_hhmm = str(data.get("sleep_cutoff_hhmm") or "").strip()
    sleep_cutoff_ts: float | None = None
    sleep_window_end_ts: float | None = None
    if cat == "sleep_vow":
        if not re.match(r"^(?:[01]?\d|2[0-3]):[0-5]\d$", cutoff_hhmm):
            cutoff_hhmm = candidate.sleep_cutoff_hhmm or "00:00"
        sleep_cutoff_ts, sleep_window_end_ts = compute_sleep_window(cutoff_hhmm, now_dt)

    deadline_ts = candidate.deadline_ts
    dl_iso = str(data.get("deadline_iso") or "").strip()
    if dl_iso:
        try:
            dt = datetime.fromisoformat(dl_iso.replace(" ", "T"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=now_dt.tzinfo)
            deadline_ts = dt.timestamp()
        except Exception:
            pass

    raw_kws = data.get("bust_keywords")
    kws: list[str] = []
    if isinstance(raw_kws, list):
        kws = [str(x).strip() for x in raw_kws if str(x).strip() and len(str(x).strip()) <= 12]
    merged_kws = tuple(dict.fromkeys([*kws, *candidate.bust_keywords]))[:10]

    return ParsedFlag(
        is_flag=True,
        category=cat,
        summary=summary,
        deadline_ts=deadline_ts,
        sleep_cutoff_ts=sleep_cutoff_ts,
        sleep_window_end_ts=sleep_window_end_ts,
        bust_keywords=merged_kws,
        confidence=conf,
    )


def match_keyword_bust(text: str, flag: FlagRecord) -> list[str]:
    if not flag.bust_keywords:
        return []
    clean = sanitize_incoming_text(text)
    if not clean or is_system_or_question(clean):
        return []
    if _THIRD_PERSON_RE.search(clean) and "我" not in _THIRD_PERSON_RE.sub("", clean):
        return []
    lower = clean.lower()
    return [kw for kw in flag.bust_keywords if kw and kw.lower() in lower]


def build_bust_verify_prompt(flag: FlagRecord, new_text: str) -> str:
    return (
        "判断同一位群友的最新发言是否确实构成对他之前所立 Flag 的“打脸 / 破戒 / 真香”，"
        "同时评估这起打脸是否属于“特别离谱/喜剧效果拉满、值得记成群聊黑历史”的名场面（例如发毒誓吃键盘/我是狗却光速破戒、发誓绝不氪却连夜648/大保底真香、半夜通宵被抓等）。\n"
        f"此前原话：{flag.original_text}\n"
        f"Flag摘要：{flag.summary}\n"
        f"最新发言：{new_text}\n\n"
        "仅当明显承认自己违背了此前立下的Flag、真香了或明确翻车时 busted 才判 true；闲聊调侃或说别人判 false。\n"
        '仅输出一行 JSON：{"busted": true/false, "outrageous": true/false, "reason": "简短原因"}'
    )


def parse_llm_bust_detail(raw_text: str) -> tuple[bool, str, bool | None]:
    data = _extract_json_block(raw_text)
    if data is None:
        return False, "", None
    busted = bool(data.get("busted", False))
    reason = str(data.get("reason") or "").strip()[:80]
    raw_out = data.get("outrageous")
    outrageous = bool(raw_out) if isinstance(raw_out, bool) else None
    return busted, reason, outrageous


def parse_llm_bust_response(raw_text: str) -> tuple[bool, str]:
    busted, reason, _ = parse_llm_bust_detail(raw_text)
    return busted, reason


_EXTREME_STAKE_RE = re.compile(
    r"(?:倒立|吃键盘|吃显示器|吃手机|我是狗|直播|发红包|请吃饭|请喝奶茶|剁手|发誓|立[帖贴]为证|话放这|毒奶|绝不)"
)
_DRAMATIC_BUST_RE = re.compile(
    r"(?:648|大保底|小保底|真香|出金|歪了|通宵|天亮|睡不着|翻车|打脸|鸽了|剁手|没忍住)"
)


@dataclass(frozen=True)
class OutrageousEval:
    is_outrageous: bool
    score: int
    reasons: tuple[str, ...]


def evaluate_outrageous_bust(
    flag: FlagRecord,
    bust_text: str,
    busted_at: float,
    *,
    llm_outrageous: bool | None = None,
    min_score: int = 4,
) -> OutrageousEval:
    score = 0
    reasons: list[str] = []

    if _EXTREME_STAKE_RE.search(flag.original_text):
        score += 3
        reasons.append("立誓含重注/狠话")

    if flag.category == "sleep_vow" and flag.sleep_cutoff_ts is not None:
        if float(busted_at) - float(flag.sleep_cutoff_ts) >= 75 * 60:
            score += 2
            reasons.append("超时熬夜严重破戒")
    elif float(busted_at) - float(flag.created_at) <= 86400:
        score += 2
        reasons.append("24小时内光速打脸")

    if _DRAMATIC_BUST_RE.search(str(bust_text or "")):
        score += 2
        reasons.append("破戒现场节目效果强")

    if llm_outrageous is True:
        score += 2
        reasons.append("模型判定值得记入黑历史")

    return OutrageousEval(
        is_outrageous=(score >= max(1, int(min_score))),
        score=score,
        reasons=tuple(reasons),
    )
