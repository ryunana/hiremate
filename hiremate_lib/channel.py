"""来源渠道（channel）枚举与额度分桶规则。

设计背景：
早期实现里，账本 `channel` 只是自由文本字段，不参与配额计算；`ledger.summary()`
把「收藏」与「主动投递」合并计入 `today_outbound_actions`，再被 `pro patrol-gate`
当作当日已用投递额度上报云端，导致：
1. `mode_discover=favorite` 收藏 N 个岗位即占用 N 个投递额度，次日真正投递时被重复计费；
2. 被动来信（HR 主动打招呼建档的 `inbound` 会话）在进入 `connected` 等状态时回填
   `opening_sent_at`，也误扣了求职者当日的「主动找岗投递额度」。

本模块把 channel 升级为一等公民：
- `outbound`：主动找岗首次破冰（消耗每日主动投递额度）；
- `inbound`：HR 主动来信的被动会话（不消耗每日主动投递额度）；
- `favorite_queue`：经 `favorite-batch` 批次状态机统一补投的候选池收尾动作
  （不消耗常规主动找岗额度，但受独立防风控总外发安全上限约束）。
"""

from __future__ import annotations

from typing import Final

#: 主动找岗首次破冰（常规主动投递，计入每日主动投递额度与找岗冷却）
CHANNEL_OUTBOUND: Final = "outbound"
#: HR 主动来信的被动会话（不占用求职者每日主动找岗额度）
CHANNEL_INBOUND: Final = "inbound"
#: 收藏队列统一补投（仅允许由 favorite-batch 状态机事务写入，不占用常规筛选投递额度）
CHANNEL_FAVORITE_QUEUE: Final = "favorite_queue"

VALID_CHANNELS: Final[tuple[str, ...]] = (
    CHANNEL_OUTBOUND,
    CHANNEL_INBOUND,
    CHANNEL_FAVORITE_QUEUE,
)

#: 不计入「每日主动找岗投递额度」的渠道：
#: - `inbound`：HR 主动来信，回复不应扣减求职者的主动找岗配额；
#: - `favorite_queue`：收藏批次补投，不占常规主动找岗配额（由总外发安全上限与云端预算兜底）。
NON_QUOTA_CHANNELS: Final[frozenset[str]] = frozenset({
    CHANNEL_FAVORITE_QUEUE,
    CHANNEL_INBOUND,
})


def normalize_channel(value: object, default: str = CHANNEL_OUTBOUND) -> str:
    """把任意输入归一化为合法 channel；未知值降级为 default，保证历史账本可读。"""
    ch = str(value or "").strip()
    return ch if ch in VALID_CHANNELS else default


def is_known_channel(value: object) -> bool:
    return str(value or "").strip() in VALID_CHANNELS


def counts_toward_quota(value: object) -> bool:
    """该 channel 的外发开场是否消耗「每日主动找岗投递额度」。"""
    return normalize_channel(value) not in NON_QUOTA_CHANNELS


def channel_label(value: object) -> str:
    """导出表格与可视化看板使用的中文短标签。"""
    ch = normalize_channel(value)
    return {
        CHANNEL_OUTBOUND: "主动检索",
        CHANNEL_INBOUND: "被动来信",
        CHANNEL_FAVORITE_QUEUE: "收藏批次补投",
    }.get(ch, ch)
