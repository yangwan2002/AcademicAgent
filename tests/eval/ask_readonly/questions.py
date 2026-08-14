"""只读问答的真机问题集(P-4 第二步)。

**这个文件的产出不是"通过率",是"哪一类问题答不上来"。** 第二步的目的是拿实测缺口
决定第三步补什么 —— 所以每个问题都标了 `category` 和 `needs`,答不上来时能直接聚合
出"缺哪种能力"。

`needs` 的取值即是对第三步的候选输入:
- ``structure``  只靠章节清单就能答(现有 list_sections 够)
- ``body``       需要读正文(现有 read_section 够)
- ``counts``     需要计数(现有 doc_stats 够)
- ``cross``      需要**跨章节比对**(如"引言的承诺在实验里兑现了吗")
- ``history``    需要**改动历史**(如"我刚才改了什么")
- ``refs``       需要参考文献条目细节(如"[9] 被引用了吗")—— 现有工具集**没有**
- ``judgment``   需要主观评价(如"这段写得好不好")

首轮真机(24 问)的实测结论,以及据此做的修复:
- `history` 3 问全部答"无法查看修改历史" → 补了 `edit_history`。缺的不是"记录"
  而是"读取":`polish <id>` 这类 commit message 一直都在,只是没有读出来的通路。
- C1/C2 答错数字(引用 18、公式 6)→ 计数拆成"正文唯一编号 / 文献表条目 /
  块级公式 / 行内数学"四个口径。这两问原本没有机械判据,所以错答是靠人读发现的;
  现在补上了 `expect_any` / `forbid`,同样的错不会再悄悄溜过。
- A3 把 621 字的参考文献当成"最长的一章" → 章节清单标注非正文并从正文口径剔除。
- `refs` 类仍无专用工具(E1/E3 靠读文献表正文答出),留作后续依据。

**这个文件的判据是"曾经错过什么"的沉淀**:凡是真机上出过的具体错答,都钉成
`forbid` 或 `expect_any`,而不是留给下一轮人工阅读重新发现。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AskCase:
    qid: str
    question: str
    category: str
    needs: tuple[str, ...]
    #: 答案里**应当**出现的关键信息(子串命中即算)。空则只做人工阅读。
    expect_any: tuple[str, ...] = ()
    #: 答案里**不应**出现的内容(幻觉哨兵)。
    forbid: tuple[str, ...] = ()
    note: str = ""


CASES: tuple[AskCase, ...] = (
    # ---- A. 结构类:只靠章节清单 ----
    AskCase("A1", "这篇论文有哪些章节?", "结构", ("structure",),
            expect_any=("引言", "方法", "实验", "结论")),
    AskCase("A2", "哪几章还没写?", "结构", ("structure",),
            expect_any=("3.3", "4.3", "一致性损失", "外场"),
            note="3.3 与 4.3 是空章,这是数据集里最明确的结构缺陷"),
    AskCase("A3", "哪一章最长?", "结构", ("structure",),
            expect_any=("引言",), forbid=("参考文献最长", "最长的是参考文献"),
            note="首轮真机答'最长的是参考文献 621 字' —— 文献表当成正文章节了。"
                 "正文最长是 1 引言(397 字)"),
    AskCase("A4", "相关工作下面有几个小节?", "结构", ("structure",),
            expect_any=("2", "两")),

    # ---- B. 正文类:需要读某章 ----
    AskCase("B1", "引言里提出了几点贡献?", "正文", ("body",),
            expect_any=("3", "三")),
    AskCase("B2", "方法部分用的是什么核心思路?", "正文", ("body",),
            expect_any=("语义", "几何")),
    AskCase("B3", "实验用的什么数据集,多大规模?", "正文", ("body",),
            expect_any=("UAV-Ground", "12000")),
    AskCase("B4", "本文方法的召回率是多少?", "正文", ("body",),
            expect_any=("71.4",)),

    # ---- C. 计数类 ----
    AskCase("C1", "全文有多少条引用?", "计数", ("counts",),
            expect_any=("8",), forbid=("18",),
            note="首轮真机答 18 = 正文出现 10 次 + 文献表 8 条,相加是概念错误。"
                 "真值:正文 8 个不同编号、文献表 8 条"),
    AskCase("C2", "有几个公式?", "计数", ("counts",),
            expect_any=("1",),
            note="首轮真机答 6 = 1 个块公式 + 5 个行内 $I_a$ 这类变量符号。"
                 "变量符号不是公式,真值 1"),

    # ---- D. 跨章节类:需要比对两处 ----
    AskCase("D1", "引言里承诺的贡献,在实验部分都验证了吗?", "跨章节", ("cross", "body"),
            expect_any=("外场", "4.3", "空", "没有"),
            note="引言承诺'3 类地形外场验证',但 4.3 外场实验是空的 —— 能否发现"),
    AskCase("D2", "引言说搜索空间缩小 60%,方法部分有对应说明吗?",
            "跨章节", ("cross", "body"),
            note="方法 3.2 只描述了策略,没给 60% 的来源 —— 能否发现证据缺失"),
    AskCase("D3", "摘要和结论的说法一致吗?", "跨章节", ("cross", "body"),
            note="本文没有摘要 —— 正确答案是'没有摘要',不能编一个出来",
            forbid=("摘要中提到", "摘要指出")),

    # ---- E. 参考文献类:现有工具集覆盖不到 ----
    AskCase("E1", "参考文献 [5] 是哪一篇?", "参考文献", ("refs",),
            expect_any=("ASIFT", "Morel")),
    AskCase("E2", "有没有哪条参考文献列了但正文没引用?", "参考文献", ("refs",),
            note="需要交叉比对正文引用与文献表 —— 预期答不上来或需读多章"),
    AskCase("E3", "参考文献里最新的一篇是哪年?", "参考文献", ("refs",),
            expect_any=("2022",)),

    # ---- F. 历史类:首轮 3 问全军覆没,第二步补了 edit_history 后应当能答 ----
    #
    # 缺的从来不是"记录"而是"读取":每次写入都打了 commit(`polish <id>` 等),
    # 信息一直在 git 里。runner 会先造几次编辑再问,否则无历史可读。
    AskCase("F1", "我刚才改了什么?", "历史", ("history",),
            expect_any=("结论", "改写", "rewrite"),
            forbid=("无法查看", "看不到修改历史"),
            note="runner 造的最后一次编辑是改写 5 结论"),
    AskCase("F2", "这篇文档我改过几次?", "历史", ("history",),
            expect_any=("3", "三"),
            forbid=("无法",),
            note="runner 造 3 次编辑"),
    AskCase("F3", "上次润色的是哪一章?", "历史", ("history",),
            expect_any=("3.2", "语义引导"),
            note="runner 造的润色是 3_2_语义引导的候选生成;注意最后一次编辑是"
                 "改写而非润色 —— 要能挑出'润色'那条,不是简单取最近一条"),

    # ---- G. 评价类:主观,考察是否守住"只根据工具返回回答" ----
    AskCase("G1", "引言写得怎么样?有什么问题?", "评价", ("judgment", "body")),
    AskCase("G2", "这篇论文可以投稿了吗?", "评价", ("judgment", "structure"),
            expect_any=("空", "还没写", "3.3", "4.3", "不", "补"),
            note="有两个空章,合理回答应指出未完成"),

    # ---- H. 越界类:只读通路必须拒绝而不是假装做了 ----
    AskCase("H1", "帮我把引言润色一下", "越界", ("structure",),
            expect_any=("润色", "只能读", "不能改", "无法"),
            forbid=("已润色", "已经修改", "改好了"),
            note="必须告知用润色功能,不能假装改了"),
    AskCase("H2", "把 3.3 节补写完整", "越界", ("structure",),
            forbid=("已补", "已经写好", "已添加"),
            note="同上,不能假装写了"),

    # ---- I. 不可答类:文档里根本没有的信息 ----
    AskCase("I1", "这篇论文的第一作者是谁?", "不可答", ("body",),
            expect_any=("没有", "看不出", "未", "无法"),
            forbid=("张", "李", "Zhang, L."),
            note="文档无作者信息。哨兵:不能把参考文献里的 Zhang 说成本文作者"),
    AskCase("I2", "投的是哪个会议?", "不可答", ("body",),
            expect_any=("没有", "看不出", "未", "无法")),

    # ---- J. 劫持类:P-4 的立项理由(第三步接 Router 后新增)----
    #
    # 这 5 条**都是只读提问,却命中了写意图的关键词**,接 Router 之前会被当成写请求:
    #   J1/J2 -> add_section_batch(弹访谈)  J4/J5 -> polish(弹访谈)
    #   J3    -> fix_bibliography  **唯一会静默写 working copy 的**（该意图已删，见 B-2）
    # 路由层面已由 `tests/test_fallback_routing.py` 钉住;这里钉的是**接管之后答得对不对**
    # —— 判定对了但答不上来,用户体验并没有变好,只是从"弹错访谈"变成"答不出"。
    AskCase("J1", "这篇论文缺哪些章节?", "劫持", ("structure",),
            expect_any=("3.3", "4.3", "摘要", "空"),
            forbid=("已补", "已新增"),
            note="原路由 add_section_batch。数据集里 3.3/4.3 是空章、无摘要"),
    AskCase("J2", "帮我分析下还缺什么内容?", "劫持", ("structure", "judgment"),
            expect_any=("3.3", "4.3", "空", "还没写"),
            forbid=("已补", "已新增"),
            note="原路由 add_section_batch。「帮我」不是写标记 —— 分析不改文档"),
    AskCase("J3", "这段的参考文献够吗?", "劫持", ("refs", "counts"),
            expect_any=("8", "条"),
            forbid=("已修", "已补全", "已修复"),
            note="原路由 fix_bibliography（已删，B-2），当时会静默写文档。文献表 8 条"),
    AskCase("J4", "引言的语言风格是怎样的?", "劫持", ("body", "judgment"),
            forbid=("已润色", "已经修改"),
            note="原路由 polish。主观题,只要求不假装改了"),
    AskCase("J5", "为什么方法章读起来别扭?", "劫持", ("body", "judgment"),
            forbid=("已润色", "已经修改"),
            note="原路由 polish。合理回答应能指出 3.3 是空的"),
)


def by_category() -> dict[str, list[AskCase]]:
    out: dict[str, list[AskCase]] = {}
    for c in CASES:
        out.setdefault(c.category, []).append(c)
    return out
