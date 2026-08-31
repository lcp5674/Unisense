"""采集链路编码乱码检测（对齐 TD §12.1「采集质量」）。

Hive/MySQL 等源库在**写入**阶段若发生编码错位（如 GBK 字节被按 UTF-8
处理），中文会被替换为 U+FFFD（替换符）或其二次转码残留，源端信息在
源头已丢失、无法还原。本模块提供轻量检测：命中时由连接器**标记**该表/字段
（而非静默入库乱码），并在采集日志/实时采样响应中告警，让问题显性化。
"""

from __future__ import annotations

#: U+FFFD（REPLACEMENT CHARACTER）——UTF-8 解码失败的标准替换符。
#: 用户侧看到的 ``2010��1��5��`` 即每个汉字被替换为 1 个 U+FFFD
#: （终端/编辑器按 2 字节宽度显示为 ``��``）。
_REPLACEMENT_CHAR = "\ufffd"

#: GBK→UTF-8 二次转码经典残留：U+FFFD 的 UTF-8 字节（EF BF BD）被按 GBK
#: 读出为「锟斤拷」等字串（连读时的常见形态）；「烫烫烫/屯屯屯」是未初始化
#: 内存被 GBK 读出的产物（C/C++ 栈未清零）。均为确定性乱码标记。
_MOJIBAKE_GBK_MARKERS = ("锟斤拷", "烫烫烫", "屯屯屯")


def contains_mojibake(text: str | None) -> bool:
    """检测文本是否含确定性乱码标记（U+FFFD 替换符或 GBK 二次转码残留）。

    只匹配**确定性**乱码（源端已被替换/损坏的标记），不做概率猜测——
    正常中文、英文、日文、emoji 均不命中；单字符 U+FFFD 也可能是有意输入，
    但出现即基本可判定为编码损坏，检测阈值取「出现 ≥1 个 U+FFFD」，
    与「合法文本几乎不含替换符」的分布一致，误报率可忽略。

    Args:
        text: 待检测文本（None/空串返回 False）。

    Returns:
        True 表示文本含确定性乱码标记。
    """
    if not text:
        return False
    if _REPLACEMENT_CHAR in text:
        return True
    return any(marker in text for marker in _MOJIBAKE_GBK_MARKERS)
