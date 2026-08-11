"""语言判定：中/英（A2a / 任务 3，Req 5）。

**判定不对称**（本轮定稿）:
- 中文靠**字符**——汉字是硬证据,检索源没给 language 字段也判 `zh`(medium);源冲突时
  仍信字符但降 low。中文判定不依赖 language 字段。
- 英文靠**源字段**——拉丁字母只能证明「非中文」,分不开 en/de/fr。故拉丁 + 源确认 en →
  `en`;拉丁 + **源缺失** → `unknown`(不硬判 en);源明确非 en 语种 → `other`。

**不把整段 Han 当中文**:日文汉字同区间,故先看假名/Hangul——有显著假名 → `ja`、
有显著 Hangul → `ko`(投影到 `quota_bucket=other`,不计中英配额)。

**标题/摘要分开算**:中文论文常配英文摘要,拼接会被英文支配。标题信号为主。

纯函数,阈值经 :class:`LanguageThresholds` **参数注入**(本模块不 import config,
保持可重复测试)。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from paper_agent.literature.untrusted import clean_external_text


@dataclass(frozen=True)
class LanguageThresholds:
    """语言判定阈值。初始值,需由 Golden Set 校准后固定(见 tasks)。"""

    han_ratio_zh: float = 0.50      # 有效字符中 Han 占比 ≥ 此值 → Han 主导
    latin_ratio: float = 0.50       # 拉丁占比 ≥ 此值 → 拉丁主导
    kana_min: float = 0.05          # 假名占比 ≥ 此值 → 显著假名（→ ja）
    hangul_min: float = 0.05        # Hangul 占比 ≥ 此值 → 显著 Hangul（→ ko）

    # 汉字**绝对数**下限：占比之外的第二条通路（满足其一即 Han 主导）。
    # 动机（2026-08 真机校准，70 条 OpenAlex 标题）：中文标题里嵌模型名/软件名会把
    # 拉丁占比顶上去 —— 「基于BERT-BiLSTM-CRF模型的中文实体识别」han 仅 0.46，
    # 纯占比规则判成 latin、漏出 zh 配额，而它明显是中文论文。
    # 定 6 的依据：实测中文标题汉字数 min=7、中位 20；纯英文标题（27 条）汉字数恒为 0，
    # 故这条通路对英文侧**零误伤**（英文标题不可能凑出 6 个汉字）。
    # 取 6 而非 4，是给「英文标题偶带一两个汉字注释」留出余量。
    han_count_zh: int = 6


DEFAULT_THRESHOLDS = LanguageThresholds()


@dataclass(frozen=True)
class LanguageVerdict:
    language_code: str                          # zh | en | ja | ko | other | unknown
    quota_bucket: str                           # zh | en | other | unknown
    confidence: str                             # high | medium | low | none
    basis: str                                  # agreement | script_analysis | source_field | none
    script_ratios: tuple[tuple[str, float], ...] = ()
    reason_codes: tuple[str, ...] = field(default_factory=tuple)


# ---- Unicode 书写系统计数 ----

def _is_han(cp: int) -> bool:
    return (
        0x4E00 <= cp <= 0x9FFF          # CJK 基本
        or 0x3400 <= cp <= 0x4DBF       # 扩展 A
        or 0x20000 <= cp <= 0x2A6DF     # 扩展 B
        or 0xF900 <= cp <= 0xFAFF       # 兼容表意
    )


def _is_hiragana(cp: int) -> bool:
    return 0x3040 <= cp <= 0x309F


def _is_katakana(cp: int) -> bool:
    return 0x30A0 <= cp <= 0x30FF


def _is_hangul(cp: int) -> bool:
    return (
        0xAC00 <= cp <= 0xD7A3          # 音节
        or 0x1100 <= cp <= 0x11FF       # Jamo
        or 0x3130 <= cp <= 0x318F       # 兼容 Jamo
    )


def _is_latin_letter(ch: str) -> bool:
    return ch.isascii() and ch.isalpha()


def _is_cyrillic(cp: int) -> bool:
    return 0x0400 <= cp <= 0x04FF


def script_counts(text: str) -> dict[str, int]:
    """按书写系统计数。数字与标点**不进分母**(只统计承载语言的字符)。"""
    counts = {"han": 0, "hiragana": 0, "katakana": 0, "hangul": 0, "latin": 0, "cyrillic": 0}
    for ch in text:
        cp = ord(ch)
        if _is_han(cp):
            counts["han"] += 1
        elif _is_hiragana(cp):
            counts["hiragana"] += 1
        elif _is_katakana(cp):
            counts["katakana"] += 1
        elif _is_hangul(cp):
            counts["hangul"] += 1
        elif _is_latin_letter(ch):
            counts["latin"] += 1
        elif _is_cyrillic(cp):
            counts["cyrillic"] += 1
    return counts


def _ratios(counts: dict[str, int]) -> dict[str, float]:
    total = sum(counts.values())
    if total == 0:
        return {k: 0.0 for k in counts}
    return {k: v / total for k, v in counts.items()}


def script_signal(text: str, thresholds: LanguageThresholds = DEFAULT_THRESHOLDS) -> str:
    """单段文本的书写系统信号:han_dominant | latin | ja | ko | cyrillic | ambiguous。

    先判假名/Hangul(它们是「非中文」的决定性信号),再判 Han/拉丁主导。
    """
    counts = script_counts(text)
    total = sum(counts.values())
    if total == 0:
        return "ambiguous"
    r = _ratios(counts)
    kana = r["hiragana"] + r["katakana"]
    if kana >= thresholds.kana_min:
        return "ja"
    if r["hangul"] >= thresholds.hangul_min:
        return "ko"
    # 占比**或**绝对数达标即 Han 主导。绝对数这条通路专治「中文标题嵌长英文缩写」，
    # 见 `LanguageThresholds.han_count_zh`；两条都不满足才继续往下判拉丁。
    if r["han"] >= thresholds.han_ratio_zh or counts["han"] >= thresholds.han_count_zh:
        return "han_dominant"
    if r["latin"] >= thresholds.latin_ratio:
        return "latin"
    if r["cyrillic"] >= thresholds.latin_ratio:
        return "cyrillic"
    return "ambiguous"


# ---- 源 language 字段归一 ----

_ZH_CODES = {"zh", "zh-cn", "zh-tw", "zh-hans", "zh-hant", "zho", "chi", "cmn", "中文", "chinese"}
_EN_CODES = {"en", "en-us", "en-gb", "eng", "english"}
_JA_CODES = {"ja", "jpn", "japanese"}
_KO_CODES = {"ko", "kor", "korean"}
_UNKNOWN_CODES = {"", "und", "unknown", "mul", "zxx"}


def normalize_language_hint(hint: str) -> str:
    """把检索源的 language 值归一为 zh | en | ja | ko | other | unknown。

    ``und`` / 空 / 未知值不猜测,归 unknown;能明确识别的非中英语种归 other。
    """
    h = (hint or "").strip().lower()
    if h in _UNKNOWN_CODES:
        return "unknown"
    if h in _ZH_CODES:
        return "zh"
    if h in _EN_CODES:
        return "en"
    if h in _JA_CODES:
        return "ja"
    if h in _KO_CODES:
        return "ko"
    return "other"          # 明确的其他语种（de/fr/…）


# ---- 融合 ----

def classify_language(
    title: str,
    abstract: str = "",
    language_hint: str = "",
    *,
    thresholds: LanguageThresholds = DEFAULT_THRESHOLDS,
) -> LanguageVerdict:
    """按 design D1 融合矩阵产出 LanguageVerdict（范围=中/英）。

    先复用不可信输入清洗,再由 **title + source** 两路信号融合。

    ``abstract`` **刻意不参与判定**（保留在签名里是为调用方与golden set 的
    形状稳定）：中文论文常配长篇英文摘要,让摘要参与会把它误判成 en。
    见 ``test_english_abstract_does_not_override_chinese_title``。
    """
    t = clean_external_text(title or "", max_chars=1000)
    title_sig = script_signal(t, thresholds)
    source = normalize_language_hint(language_hint)
    ratios = tuple(sorted(_ratios(script_counts(t)).items()))

    # 1) 假名/Hangul/Cyrillic → 非中英目标语言
    if title_sig == "ja":
        return LanguageVerdict("ja", "other", "medium", "script_analysis", ratios, ("kana_present",))
    if title_sig == "ko":
        return LanguageVerdict("ko", "other", "medium", "script_analysis", ratios, ("hangul_present",))
    if title_sig == "cyrillic":
        return LanguageVerdict("other", "other", "medium", "script_analysis", ratios, ("cyrillic",))

    # 2) Han 主导 → 中文，且**置信恒为 high**：汉字占比 ≥50% 基本不可能是误判，
    #    而源 language 字段实测极不可靠。2026-08 真机实测（OpenAlex 4 个中文查询、
    #    31 条中文标题文献）：字段为 en 的 16 条、ja 4 条、ru 1 条、空缺 4 条，
    #    只有 6/31 标对 —— 准确率约 19%，其中「结合图论的异源影像点云配准方法」
    #    这类纯中文标题被标成 ja。
    #
    #    旧实现把源字段缺失降 medium、冲突降 low，于是默认策略 high_only（只计 high）
    #    下仅 6/31 能计入 zh 配额，`zh>=5` 这类要求**恒定失败**、检索白跑。
    #    降置信名义上是保守，方向其实反了：那等于宁信一个 81% 出错的外部字段，
    #    也不信眼前的汉字。故这里让硬证据说话，冲突/缺失只记 reason 供审计，不降置信。
    if title_sig == "han_dominant":
        if source == "zh":
            return LanguageVerdict("zh", "zh", "high", "agreement", ratios)
        if source == "unknown":
            return LanguageVerdict("zh", "zh", "high", "script_analysis", ratios,
                                   ("source_missing",))
        return LanguageVerdict("zh", "zh", "high", "script_analysis", ratios,
                               ("script_source_conflict",))

    # 3) 拉丁主导 → 英文靠源字段
    if title_sig == "latin":
        if source == "en":
            return LanguageVerdict("en", "en", "high", "agreement", ratios)
        if source == "other" or source in ("ja", "ko"):
            # 源明确标非英语种 → other，不计中英配额
            return LanguageVerdict("other", "other", "medium", "source_field", ratios, ("source_non_en",))
        # 源缺失/无法确定 → 拉丁分不出 en/de/fr，不硬判 en
        return LanguageVerdict("unknown", "unknown", "none", "none", ratios, ("latin_no_source",))

    # 4) 标题信号 ambiguous → 退到源字段
    if source == "zh":
        return LanguageVerdict("zh", "zh", "low", "source_field", ratios, ("title_ambiguous",))
    if source == "en":
        return LanguageVerdict("en", "en", "low", "source_field", ratios, ("title_ambiguous",))
    if source in ("ja", "ko", "other"):
        return LanguageVerdict("other", "other", "low", "source_field", ratios, ("title_ambiguous",))
    return LanguageVerdict("unknown", "unknown", "none", "none", ratios, ("no_evidence",))


# ---- 计数策略 ----

def counts_toward(verdict: LanguageVerdict, target_bucket: str, policy: str) -> bool:
    """该 verdict 是否计入 target_bucket(zh/en)的语言配额。

    - ``high_only``:**只计 high**（名副其实,不含 medium）。
    - ``include_low_with_warning``:high/medium/low 均计入(调用方须报告低置信数)。
    - other/unknown/none 恒不计入。
    """
    if verdict.quota_bucket != target_bucket:
        return False
    if target_bucket not in ("zh", "en"):
        return False
    conf = verdict.confidence
    if conf == "none":
        return False
    if policy == "high_only":
        return conf == "high"
    if policy == "include_low_with_warning":
        return conf in ("high", "medium", "low")
    return conf == "high"       # 未知 policy → 保守


def classify_record_language(record, thresholds: LanguageThresholds = DEFAULT_THRESHOLDS) -> LanguageVerdict:
    """对 ReferenceRecord 判语言(A2b 显式传运行时阈值,避免隐式全局状态)。"""
    return classify_language(
        record.title, record.abstract, record.language_hint, thresholds=thresholds
    )


__all__ = [
    "LanguageThresholds",
    "DEFAULT_THRESHOLDS",
    "LanguageVerdict",
    "script_counts",
    "script_signal",
    "normalize_language_hint",
    "classify_language",
    "counts_toward",
    "classify_record_language",
]
