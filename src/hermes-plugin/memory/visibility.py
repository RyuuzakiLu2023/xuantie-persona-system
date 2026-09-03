# -*- coding: utf-8 -*-
"""XTMEM 记忆可见性三态判定——全库唯一实现。

R1.3（2026-08-31）：CLI / provider / curator 一律调本函数，禁止各写一遍。
语义：
- shared        → 全可见
- private       → 仅 chunk_namespace == viewer
- shared-with:a,b → list 名单内或 chunk_namespace == viewer
- 无 viewer / 无 visibility → True（CLI 上帝视角 / 历史数据兜底）
"""


def visibility_ok(visibility: str, chunk_namespace: str, viewer: str) -> bool:
    """三态判定，全库唯一实现。CLI / provider / curator 一律调它，禁止各写一遍。"""
    vis = visibility or "shared"
    if not viewer or vis == "shared":
        return True
    if vis == "private":
        return chunk_namespace == viewer
    if vis.startswith("shared-with:"):
        allowed = [x.strip() for x in vis[len("shared-with:"):].split(",") if x.strip()]
        return chunk_namespace == viewer or viewer in allowed
    return True


def min_visibility(source_ids, lookup=None):
    """取所有来源的最严可见性（全库唯一实现）：private > shared-with > shared。

    P0.2（2026-08-31）：蒸馏/摘要/预判等衍生记忆的可见性 = 全部来源的最严者。
    - lookup(chunk_id) -> visibility（None 视为 unknown）
    - 来源 id 查不到 / unknown -> 按最严 private 兜底（fail-closed，禁回落 shared）
    - 空 source_ids -> private（无血缘的衍生物一律最保守）
    """
    if not source_ids:
        return "private"
    best_sw = None
    for sid in source_ids:
        # BF-3（2026-09-01）：查不到/unknown → 按最严 private 兜底（fail-closed，禁回落 shared）。
        # 上一版 `or "shared"` 与自身 docstring 矛盾，AF-1 解锁血缘后蒸馏/画像传已删源 id 会静默降级 shared。
        v = (lookup(sid) if lookup else None) or "private"
        if v == "private":
            return "private"
        if v.startswith("shared-with:"):
            if best_sw is None:
                best_sw = v
    return best_sw or "shared"
