# `maibot-flag-boomerang`（`github.yunshanluan0714.flag-boomerang`） (v1.1.0) — 群聊 Flag 回旋镖追踪器

平时**零存在感静默记账**、常态 **0 Prompt 干扰**，只在群友立下早睡誓言、戒氪毒誓、限时 Flag 或毒奶狠话后真的破戒或到期时，顺手精准递出“回旋镖”；**特别离谱的打脸名场面还支持自动写入 MaiBot 原生长期记忆与向量库**。

## 核心特性

1. **两级低成本高精度捕获**：
   - 本地高速正则初筛，过滤问句、媒体占位与系统提醒；
   - 仅对高疑似候选调用 `fast_model` 复核并抽取时间/打脸词，失败优雅回落纯规则。
2. **三种回旋镖触发方式**：
   - **早睡抓现行**：立誓早睡后过了截止时间（带 15 分钟宽限）仍在发消息即标记打脸；若已过 `01:00` 静默线则先暂存，白天冒泡时再清算；
   - **真香关键词复核**：匹配到抽卡/氪金/喝奶茶等反面词，经复核确认后下次回复顺嘴递梗；
   - **到期验收巡检**：约定时间到期后在群活跃且未达每日限额时主动验收，冷群则转为等待当事人冒泡再追问。
3. **离谱名场面自动写入长期记忆（内置可选宿主补丁）**：
   - 对“含重注狠话 + 严重熬夜或 24 小时内光速打脸 / 强节目效果 / 模型判定离谱”的打脸事件自动评分（默认 `>= 4` 分），通过 `memory.ingest_text` 幂等写入 MaiBot 原生 `A_memorix` 长期记忆与向量库；普通日常小事不污染记忆库。
   - **零补丁兼容**：未安装下方可选补丁的原生 MaiBot 可直接安装运行，所有 SQLite 记账、早睡/真香/到期抓现行均 100% 正常工作，长期记忆写入步骤会自动静默跳过。
4. **严格边界**：
   - SQLite 严格按 `group_id`（私聊按 `private:<user_id>`）物理隔离，绝不跨群串瓜；
   - 常态聊天不往模型提示词注入任何字句。
5. **自然语言管理**：
   - `flag_boomerang_query`：查本群进行中或已打脸的历史记录；
   - `flag_boomerang_manage`：手动登记 Flag、兑现销账或作废误记。

## 开启「离谱名场面写入长期记忆」（可选）

原生 MaiBot 插件运行时默认未向插件暴露 `memory.ingest_text` 写入接口。本仓库附带了幂等宿主补丁脚本 [`patches/apply_memory_ingest_cap_patch.py`](patches/apply_memory_ingest_cap_patch.py)（适配 MaiBot `1.4.x`），只需在 MaiBot 根目录或容器内执行一次并重启即可开启：

```bash
# 为宿主注册 memory.ingest_text 长期记忆写入能力（幂等，可重复执行）
python patches/apply_memory_ingest_cap_patch.py --maibot-root /path/to/maibot

# 检查补丁状态
python patches/apply_memory_ingest_cap_patch.py --maibot-root /path/to/maibot --check
```

若不希望改动宿主代码，无需执行上述脚本，直接安装插件即可正常使用全部回旋镖追踪与抓现行功能。

## 运行单元测试

```bash
python -m unittest discover -p "test_*.py"
```

## 开源协议

本项目基于 [MIT License](LICENSE) 开源。
