"""来源渠道（channel）枚举与额度分桶规则。

背景：早期实现里，账本 `channel` 只是一个自由文本字段，只用于导出展示，不参与任何
额度计算。而 `ledger.summary()` 把「收藏」与「主动投递」都累加进同一个
`today_outbound_actions`，再被 `pro patrol-gate` 当作当日已用配额上报云端，导致：

1. `mode_discover=favorite` 收藏 N 个岗位后，当日投递额度被占用 N 个；
2. 用户隔天把这 N 个收藏真正投出去时，同一批岗位被重复计费；
3. "非工作时间只收藏、工作时间窗到点统一投递"这一真实工作流无法成立——因为配额
   在还没发出任何破冰消息前就已经耗尽。

本模块把 channel 升级为一等公民：只有「真正对外发出消息」的渠道才消耗投递额度，
收藏/候选池动作与批量补投各有独立语义。
"""

from __future__ import annotations

from typing import Final

#: 主动找岗首次破冰（常规外部投递，计入每日额度与冷却）
CHANNEL_OUTBOUND: Final = "outbound"
#: HR 主动来信后的被动会话（用户无需为自己的信计费，但也不占主动投递额度）
CHANNEL_INBOUND: Final = "inbound"
#: 收藏队列统一补投：由 favorite_batch 到点后批量执行，属于用户已批准的延迟动作，
#: 不占当日筛选投递额度、不影响常规找岗冷却。
CHANNEL_FAVORITE_QUEUE: Final = "favorite_queue"

VALID_CHANNELS: Final[tuple[str, ...]] = (
    CHANNEL_OUTBOUND,
    CHANNEL_INBOUND,
    CHANNEL_FAVORITE_QUEUE,
)

#: 不计入「每日主动投递额度」的渠道。
#:
#: `favorite_queue` 之所以豁免：批次里的岗位是在此前轮次中已被收藏（候选池动作）的，
#: 到点统一投递是该动作的收尾，而不是新一轮筛选投递；若重复计费，用户会在同一天为
#: 同一批岗位被扣两次额度，直接违背"少而精"的产品定位。
NON_QUOTA_CHANNELS: Final[frozenset[str]] = frozenset({CHANNEL_FAVORITE_QUEUE})


def normalize_channel(value: object, default: str = CHANNEL_OUTBOUND) -> str:
    """把任意输入归一化成合法 channel；未知值降级为 default 而不是抛错。

    刻意不抛错：历史账本里可能存在早期自由填写的渠道值，升级后不应因此打不开账本。
    """
    ch = str(value or "").strip()
    return ch if ch in VALID_CHANNELS else default


def is_known_channel(value: object) -> bool:
    return str(value or "").strip() in VALID_CHANNELS


def counts_toward_quota(value: object) -> bool:
    """该 channel 的实际外发消息是否计入每日主动投递额度。"""
    return normalize_channel(value) not in NON_QUOTA_CHANNELS


def channel_label(value: object) -> str:
    """导出与看板用的中文短标签。"""
    ch = normalize_channel(value)
    return {
        CHANNEL_OUTBOUND: "主动找岗",
        CHANNEL_INBOUND: "被动来信",
        CHANNEL_FAVORITE_QUEUE: "收藏批次补投",
    }.get(ch, ch)
