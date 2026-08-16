"""问题归一化（确定性规则，纯函数）。

第一版只做确定性归一化（《数据对象设计》§9）：
1. Unicode 规范化；2. 去首尾空白；3. 连续空白压缩为一个空格；
4. 英文字母转小写；5. 统一常见中英文标点；6. 删除仅影响句尾的问号和句号；
7. 不改写实体、数字、产品代码、订单号和错误码。

归一化后为空的请求必须拒绝（EMPTY_QUESTION）。
"""

import hashlib
import re
import unicodedata

# 常见中英文标点统一为半角/统一形式
_PUNCT_MAP = {
    "，": ",",
    "。": ".",
    "；": ";",
    "：": ":",
    "！": "!",
    "？": "?",
    "（": "(",
    "）": ")",
    "【": "[",
    "】": "]",
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
    "、": ",",
}

_WHITESPACE_RE = re.compile(r"\s+")
# 设计只要求删除句尾问号和句号（含 NFKC/标点映射后的半角 ? .），保留感叹号
_TRAILING_Q_RE = re.compile(r"[?.]+$")


def normalize_question(raw: str) -> str:
    """返回归一化问题；空输入返回空串（由调用方拒绝）。"""
    if raw is None:
        return ""
    # 1. Unicode 规范化
    text = unicodedata.normalize("NFKC", raw)
    # 5. 统一常见中英文标点
    text = "".join(_PUNCT_MAP.get(ch, ch) for ch in text)
    # 2/3. 去首尾空白、压缩连续空白
    text = _WHITESPACE_RE.sub(" ", text).strip()
    # 4. 英文字母转小写
    text = text.lower()
    # 6. 删除仅影响句尾的问号和句号
    text = _TRAILING_Q_RE.sub("", text)
    return text


def question_hash(normalized: str) -> str:
    """归一化问题 SHA-256。"""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()
