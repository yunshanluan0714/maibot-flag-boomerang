"""Configuration schema for github.yunshanluan0714.flag-boomerang v1.1.0."""

from maibot_sdk import Field, PluginConfigBase


class PluginSection(PluginConfigBase):
    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="启用群聊 Flag 回旋镖追踪器")
    config_version: str = Field(default="1.1.0", description="配置版本")


class CaptureSection(PluginConfigBase):
    __ui_label__ = "自动捕获设置"
    __ui_icon__ = "flag"
    __ui_order__ = 1

    enable_auto_capture: bool = Field(default=True, description="自动静默捕捉群友立Flag、毒奶与早睡誓言")
    use_llm_verification: bool = Field(default=True, description="对候选消息调用小模型复核并抽取时间/打脸关键词")
    llm_task_name: str = Field(default="fast_model", description="复核与判定使用的模型任务路由名")
    min_confidence: float = Field(default=0.72, ge=0.3, le=0.99, description="最低入库置信度阈值")
    max_active_per_user: int = Field(default=5, ge=1, le=20, description="单群内每人最多同时保留的待验 Flag 数量")
    allow_private: bool = Field(default=True, description="是否同时在私聊中各自独立生效")
    allowed_groups: list[str] = Field(default_factory=list, description="仅在指定群号列表启用（留空表示全部群启用）")
    blocked_groups: list[str] = Field(default_factory=list, description="黑名单群号列表（优先于白名单跳过）")


class TriggerSection(PluginConfigBase):
    __ui_label__ = "回旋镖触发与静默"
    __ui_icon__ = "bell"
    __ui_order__ = 2

    enable_sleep_bust: bool = Field(default=True, description="立了早睡 Flag 后若超时仍在发言，触发熬夜抓现行")
    enable_keyword_bust: bool = Field(default=True, description="检测到明显打脸发言时自动核算并触发回旋镖")
    enable_deadline_check: bool = Field(default=True, description="定期检查约定时间已到期的 Flag")
    proactive_daily_limit_per_group: int = Field(default=2, ge=0, le=10, description="单群每天最多主动发回旋镖次数")
    proactive_cooldown_minutes: int = Field(default=30, ge=5, le=720, description="同一群两次主动触发最小冷却分钟数")
    quiet_start: str = Field(default="01:00", description="深夜免打扰开始时间（HH:MM），期间只记账不发消息")
    quiet_end: str = Field(default="08:00", description="深夜免打扰结束时间（HH:MM）")
    timezone: str = Field(default="Asia/Shanghai", description="判定时段与日期的时区")
    save_outrageous_to_memory: bool = Field(default=True, description="将特别离谱的打脸名场面自动写入 MaiBot 长期记忆与向量库")
    outrageous_min_score: int = Field(default=4, ge=2, le=8, description="写入长期记忆所需的最低离谱评分阈值")


class FlagBoomerangConfig(PluginConfigBase):
    plugin: PluginSection = Field(default_factory=PluginSection)
    capture: CaptureSection = Field(default_factory=CaptureSection)
    trigger: TriggerSection = Field(default_factory=TriggerSection)
