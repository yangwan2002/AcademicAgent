"""可信文献库（spec A / 阶段 A1）。

把「文献」从两个各自为政的贫血结构（``ReferenceEntry`` 检索用、``KnownRef`` 持久化用）
收敛成一个有身份、有核验审计、可去重、可迁移的持久化实体。

A1 的模块分工：

- ``identifiers``  ：标识符归一化与分类；**作品级 / 载体级严格分离**
- ``sources``      ：检索源角色、来源层级、标识符类型级核验权限
- ``verification`` ：只追加核验事件流 + 状态折叠派生
- ``models``       ：``ReferenceRecord``（唯一可写、唯一持久化的文献实体）
- ``dedup``        ：基于作品级标识符的去重与元数据合并
- ``bibkey``       ：bibkey 生成
- ``ledger``       ：``ReferenceLedger``（唯一修改入口 + 五套索引）
- ``migration``    ：旧 facts 无损迁移与原子写回
"""
