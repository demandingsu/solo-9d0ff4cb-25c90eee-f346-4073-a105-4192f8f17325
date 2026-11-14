"""人工复核名单抽取：按 UTC 日期 × 检验结论分组配额的样品唯一分配。

输入是某门户某区间的**已发布**报告（published_records）。每个分组
``(UTC 日期, 结论)`` 有自己的配额；同一样品在整份名单中最多出现一次，
因此当同一样品在不同日期或两类结论下都有报告时，各组配额必须联合求解，
不能逐组贪心。

算法：分组按固定顺序（日期、结论）处理，组内报告按由种子派生的确定性
随机序排列，再用带回溯的深度优先搜索为每个分组凑满配额；样品被某组占用
后其他组只能改选，后续分组凑不满时失败会跨组回滚。DFS 完备性保证：
- 存在足额方案 ⇒ 一定找到，返回完整名单，绝不输出部分名单；
- 不存在 ⇒ 抛 QuotaConflict，逐组给出配额与可用样品数等冲突说明。

随机种子决定候选顺序：同一发布版本（同候选集）与同一种子，洗牌结果与
深度优先找到的第一个方案都完全一致，因此名单可复现。
"""
from __future__ import annotations

import hashlib
import random
import sys
from collections import OrderedDict

from . import db
from .timeutil import fmt_ts

RESULTS = ("合格", "不合格")
DAY_SECONDS = 86400
# 防御性上限：极端冲突结构下避免指数级回溯；正常质控规模远触不到。
MAX_SEARCH_ATTEMPTS = 200_000
# 回溯深度随候选尝试次数增长，显式放宽解释器栈上限（配合 MAX_SEARCH_ATTEMPTS 有界）。
sys.setrecursionlimit(max(10_000, MAX_SEARCH_ATTEMPTS // 4 + 1000))


class SamplingError(Exception):
    """请求本身不合法（区别于配额冲突）。"""


class QuotaConflict(Exception):
    """不存在足额方案：携带每组可用性说明，不允许输出部分名单。"""

    def __init__(self, groups: list[dict], total_quota: int, total_available: int):
        self.groups = groups
        self.total_quota = total_quota
        self.total_available = total_available
        super().__init__("配额冲突")


def _day_key(ts: int) -> str:
    """epoch 秒对应的 UTC 日期，返回 YYYY-MM-DD。"""
    return fmt_ts(ts)[:10]


def _seeded_rng(seed: int, day: str, result: str) -> random.Random:
    """由总种子 + 分组身份派生独立 RNG，保证分组处理顺序不影响组内随机序。"""
    digest = hashlib.sha256(f"{seed}|{day}|{result}".encode("utf-8")).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _sample_key(sample_no, report_no: str) -> str:
    """样品号为空时回退为仅属于该报告的唯一样品（同一份报告不会被抽两次，
    但不同报告天然互异，不参与跨组协调）。"""
    if sample_no is None or str(sample_no).strip() == "":
        return f"\0report-only:{report_no}"
    return str(sample_no)


def _build_groups(rows, quotas: dict[tuple[str, str], int], seed: int):
    """把已发布记录整理成有序分组结构。

    返回 OrderedDict: (day, result) -> {
        quota, candidates: [{report_no, sample_no, sample_key, report_time, result, day}]
    }，候选已按种子随机序排列；同一报告在分组内只出现一次。
    顺序按 (日期, 结论) 固定，保证确定性。
    """
    grouped: dict[tuple[str, str], list[dict]] = {}
    for r in rows:
        day = _day_key(r["report_time"])
        key = (day, r["result"])
        if key not in quotas:
            continue  # 未给配额的日期/结论不参与抽取
        grouped.setdefault(key, []).append(
            {
                "report_no": r["report_no"],
                "sample_no": r["sample_no"],
                "sample_key": _sample_key(r["sample_no"], r["report_no"]),
                "report_time": r["report_time"],
                "result": r["result"],
                "day": day,
            }
        )

    ordered = OrderedDict()
    # 分组次序固定为 (日期, 结论序：合格→不合格)，不依赖中文字典序，
    # 保证跨环境确定且回溯搜索空间一致。
    for key in sorted(quotas, key=lambda k: (k[0], RESULTS.index(k[1]))):
        candidates = grouped.get(key, [])
        rng = _seeded_rng(seed, key[0], key[1])
        # 先按稳定全序去重（理论上 report_no 唯一，这里防御重复数据），
        # 再用种子洗牌决定候选顺序。
        unique = list({c["report_no"]: c for c in candidates}.values())
        unique.sort(key=lambda c: (c["report_time"], c["report_no"]))
        rng.shuffle(unique)
        ordered[key] = {"quota": quotas[key], "candidates": unique}
    return ordered


def _group_sample_sets(groups) -> dict[tuple[str, str], set]:
    """每个分组内去重后的候选样品集合（以 sample_key 计）。"""
    sets = {}
    for gkey, g in groups.items():
        seen = {cand["sample_key"] for cand in g["candidates"]}
        sets[gkey] = seen
    return sets


def _solve(groups: "OrderedDict[tuple[str, str], dict]"):
    """带回溯的确定性配额分配（深度优先搜索）。

    分组按固定顺序处理，组内候选（样品的代表报告）按种子随机序依次尝试；
    每个样品整份名单只能被一个分组占用，当前分组凑不满配额时，失败沿递归
    栈返回到前面的分组改选其他样品（跨组回滚）。“分组顺序 + 组内候选顺序”
    都由输入和种子唯一确定，因此深度优先找到的第一个足额方案可复现。
    模块加载时已放宽解释器递归上限，并由 MAX_SEARCH_ATTEMPTS 兜底，避免
    极端冲突结构下递归过深或回溯失控。

    返回 (feasible, chosen_by_group)：
    - feasible=True 时 chosen_by_group 为每组选中的代表报告（按组内随机序）；
    - feasible=False 时 chosen_by_group 为空 dict，调用方据此走冲突分支，
      绝不产出部分名单。
    """
    group_keys = list(groups.keys())
    sample_sets = _group_sample_sets(groups)

    # 廉价不可行预检：任一单组配额超过本组去重样品数，或总配额超过跨组
    # 去重样品总数，必然无解，直接判定避免大规模回溯。
    total_available = set().union(*sample_sets.values()) if sample_sets else set()
    total_quota = sum(groups[g]["quota"] for g in group_keys)
    if any(groups[g]["quota"] > len(sample_sets[g]) for g in group_keys) \
            or total_quota > len(total_available):
        return False, {}

    # 每个样品在每个分组中的代表报告（候选已按随机序排列，取第一条去重）。
    candidates_by_group: dict[tuple[str, str], list[dict]] = {}
    for gkey in group_keys:
        seen, reps = set(), []
        for cand in groups[gkey]["candidates"]:
            if cand["sample_key"] in seen:
                continue
            seen.add(cand["sample_key"])
            reps.append(cand)
        candidates_by_group[gkey] = reps

    chosen: dict[tuple[str, str], list[dict]] = {gkey: [] for gkey in group_keys}
    used: set[str] = set()
    state = {"attempts": 0}

    def backtrack(gi: int, pos: int) -> bool:
        """在第 gi 组、从候选位置 pos 起按随机序凑满本组配额，再进入下一组。

        每个“选/跳过当前候选”的决定都占递归栈一层，后续分组凑不满时失败
        会一路返回并尝试另一决定，从而实现跨组回滚。递归深度由
        MAX_SEARCH_ATTEMPTS 与放宽后的 recursionlimit 共同约束。
        """
        if gi == len(group_keys):
            return True  # 所有分组均已凑满
        gkey = group_keys[gi]
        if len(chosen[gkey]) == groups[gkey]["quota"]:
            return backtrack(gi + 1, 0)
        if pos >= len(candidates_by_group[gkey]):
            return False  # 本组候选耗尽仍未满额，回溯到前面的分组改选

        state["attempts"] += 1
        if state["attempts"] > MAX_SEARCH_ATTEMPTS:
            raise RuntimeError("配额组合搜索超出上限")
        cand = candidates_by_group[gkey][pos]
        if cand["sample_key"] not in used:
            # 决定一：选当前候选，继续在本组挑下一个位置。
            used.add(cand["sample_key"])
            chosen[gkey].append(cand)
            if backtrack(gi, pos + 1):
                return True
            chosen[gkey].pop()
            used.discard(cand["sample_key"])
        # 决定二：跳过当前候选（或它已被占用），尝试同组后续候选。
        return backtrack(gi, pos + 1)

    feasible = backtrack(0, 0)
    return (True, chosen) if feasible else (False, {})


def run_sampling(portal_url: str, start_ts: int, end_ts: int,
                 quotas: dict[tuple[str, str], int], seed: int) -> dict:
    """在单一数据库快照内完成候选读取、配额判定与版本信息采集。"""
    with db.connect() as conn:
        # 显式开启读事务：Python sqlite3 默认只在写语句时隐式开事务，
        # 纯 SELECT 会各自自动提交、读到最新数据，无法保证同一快照。
        # BEGIN 之后本事务内所有读取固定在同一快照——期间即使有重采发布
        # （另一写事务整体替换 publications），候选报告、配额判定、批次与
        # 任务号仍全部来自抽取开始时的数据库版本。
        conn.execute("BEGIN")
        pub = conn.execute(
            "SELECT * FROM publications WHERE portal_url = ? AND start_ts = ? AND end_ts = ?",
            (portal_url, start_ts, end_ts),
        ).fetchone()
        if pub is None:
            raise SamplingError("该门户及区间暂无已发布结果，无法抽取")
        rows = conn.execute(
            "SELECT report_no, sample_no, report_time, result FROM published_records"
            " WHERE publication_id = ?",
            (pub["id"],),
        ).fetchall()

        groups = _build_groups(rows, quotas, seed)
        feasible, chosen = _solve(groups)

        total_quota = sum(g["quota"] for g in groups.values())
        if not feasible:
            sample_sets = _group_sample_sets(groups)
            conflict_groups = []
            for gkey, g in groups.items():
                available = len(sample_sets[gkey])
                conflict_groups.append(
                    {
                        "date": gkey[0],
                        "result": gkey[1],
                        "quota": g["quota"],
                        "available_samples": available,
                        "shortfall": max(0, g["quota"] - available),
                    }
                )
            total_available = len(set().union(*sample_sets.values()))
            raise QuotaConflict(conflict_groups, total_quota, total_available)

        # 足额方案：按分组顺序与组内随机序输出，名单顺序同样确定。
        selected_reports = []
        per_group = []
        for gkey, g in groups.items():
            picked = chosen[gkey]
            per_group.append(
                {
                    "date": gkey[0],
                    "result": gkey[1],
                    "quota": g["quota"],
                    "selected": len(picked),
                }
            )
            for cand in picked:
                selected_reports.append(
                    {
                        "report_no": cand["report_no"],
                        "sample_no": cand["sample_no"],
                        "report_time": fmt_ts(cand["report_time"]),
                        "date": cand["day"],
                        "result": cand["result"],
                        "batch": pub["batch"],
                        "task_id": pub["task_id"],
                    }
                )

    return {
        "portal_url": pub["portal_url"],
        "start": fmt_ts(pub["start_ts"]),
        "end": fmt_ts(pub["end_ts"]),
        "seed": seed,
        "publication": {
            "id": pub["id"],
            "batch": pub["batch"],
            "task_id": pub["task_id"],
            "record_count": pub["record_count"],
            "published_at": pub["published_at"],
        },
        "groups": per_group,
        "selected_count": len(selected_reports),
        "selected_reports": selected_reports,
    }
