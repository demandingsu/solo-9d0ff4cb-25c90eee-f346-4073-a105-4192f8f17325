#!/usr/bin/env python3
"""抽检 API 端到端验证：跨组样品协调、配额冲突、随机确定性与发布版本快照。

用法：
    python3 scripts/verify_sampling.py

环境变量：
    API_BASE           API 地址（默认 http://localhost:8000）
    PORTAL_BASE        门户地址（默认 http://localhost:8001）
    PORTAL_URL_FOR_API API 访问门户所用的 URL（默认 http://portal:8000，即 compose 网络）
"""
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

API_BASE = os.environ.get("API_BASE", "http://localhost:8000")
PORTAL_BASE = os.environ.get("PORTAL_BASE", "http://localhost:8001")
PORTAL_URL = os.environ.get("PORTAL_URL_FOR_API", "http://portal:8000")

RANGE_START = "2024-01-01T00:00:00Z"
RANGE_END = "2024-01-03T00:00:00Z"

failures = []


def req(method, base, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        base + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {"raw": raw}
        return exc.code, payload


def check(name, cond, extra=""):
    print(("  [OK] " if cond else "  [FAIL] ") + name + ("" if cond else f" {extra}"))
    if not cond:
        failures.append(name)


def section(title):
    print(f"\n== {title} ==")


def sample_body(seed, quotas):
    return {
        "portal_url": PORTAL_URL,
        "start": RANGE_START,
        "end": RANGE_END,
        "seed": seed,
        "quotas": quotas,
    }


# 跨日期/跨结论的共享样品（同一样品在多个分组都有报告）：
#   X1: 1/1 合格、1/2 合格        X2: 1/1 合格、1/1 不合格
#   X3: 1/1 不合格                X4: 1/2 合格
#   X5: 1/2 不合格
SHARED_REPORTS = [
    {"sample_no": "SAM-X1", "report_time": "2024-01-01T08:00:00Z", "result": "合格"},
    {"sample_no": "SAM-X1", "report_time": "2024-01-02T09:00:00Z", "result": "合格"},
    {"sample_no": "SAM-X2", "report_time": "2024-01-01T10:00:00Z", "result": "合格"},
    {"sample_no": "SAM-X2", "report_time": "2024-01-01T11:00:00Z", "result": "不合格"},
    {"sample_no": "SAM-X3", "report_time": "2024-01-01T12:00:00Z", "result": "不合格"},
    {"sample_no": "SAM-X4", "report_time": "2024-01-02T13:00:00Z", "result": "合格"},
    {"sample_no": "SAM-X5", "report_time": "2024-01-02T14:00:00Z", "result": "不合格"},
]


def main():
    section("0. 健康检查")
    code, _ = req("GET", API_BASE, "/healthz")
    check("API 健康", code == 200)
    code, _ = req("GET", PORTAL_BASE, "/healthz")
    check("门户健康", code == 200)
    if failures:
        return summary()

    section("1. 造跨组共享样品并补采发布")
    req("POST", PORTAL_BASE, "/admin/reset")
    code, body = req("POST", PORTAL_BASE, "/admin/seed-reports", {"reports": SHARED_REPORTS})
    check("显式造数 7 条", code == 201 and body.get("inserted") == 7, body)
    code, task = req("POST", API_BASE, "/tasks",
                     {"portal_url": PORTAL_URL, "start": RANGE_START, "end": RANGE_END})
    check("补采完成并发布", code == 201 and task["status"] == "completed", task.get("status"))
    check("发布批次 BATCH-0001", task["publication"]["batch"] == "BATCH-0001",
          task["publication"])
    pub_task_1 = task["publication"]["task_id"]

    section("2. 参数校验")
    code, _ = req("POST", API_BASE, "/samples", sample_body(1, []))
    check("空配额返回 400", code == 400)
    code, _ = req("POST", API_BASE, "/samples",
                  sample_body(1, [{"date": "2024-02-01", "qualified": 1}]))
    check("配额日期与区间无交集返回 400", code == 400)
    code, _ = req("POST", API_BASE, "/samples",
                  sample_body(1, [{"date": "2024/01/01", "qualified": 1}]))
    check("非法日期返回 400", code == 400)
    code, _ = req("POST", API_BASE, "/samples",
                  sample_body(1, [{"date": "2024-01-01", "qualified": -1}]))
    check("负配额返回 422", code == 422)
    code, _ = req("POST", API_BASE, "/samples", sample_body(1, [
        {"date": "2024-01-01", "qualified": 1},
        {"date": "2024-01-01", "unqualified": 1},
    ]))
    check("重复日期返回 400", code == 400)
    code, _ = req("POST", API_BASE, "/samples", {
        "portal_url": PORTAL_URL,
        "start": "2025-01-01T00:00:00Z", "end": "2025-01-02T00:00:00Z",
        "seed": 1, "quotas": [{"date": "2025-01-01", "qualified": 1}],
    })
    check("未发布区间返回 404", code == 404)

    section("3. 跨组协调的足额抽取")
    # 配额共 5 份 = 去重样品总数；1/1 合格只能选 1 个，把 X1 让给只含 X1/X4 的
    # 1/2 合格之外的可行分配——必须联合求解，逐组贪心会失败。
    quotas_ok = [
        {"date": "2024-01-01", "qualified": 1, "unqualified": 2},
        {"date": "2024-01-02", "qualified": 1, "unqualified": 1},
    ]
    code, body = req("POST", API_BASE, "/samples", sample_body(42, quotas_ok))
    check("足额抽取 200", code == 200, body)
    reports = body.get("selected_reports", [])
    check("抽满 5 份", body.get("selected_count") == 5 and len(reports) == 5,
          body.get("selected_count"))
    sample_nos = [r["sample_no"] for r in reports]
    check("同一样品只选一份报告", len(set(sample_nos)) == len(sample_nos) == 5, sample_nos)
    check("每条都带发布批次与任务号",
          all(r["batch"] == "BATCH-0001" and r["task_id"] == pub_task_1 for r in reports))
    check("日期与结论归属正确", all(r["date"] == r["report_time"][:10] for r in reports))
    # 各组配额都被满足
    group_counts = {}
    for r in reports:
        group_counts[(r["date"], r["result"])] = group_counts.get((r["date"], r["result"]), 0) + 1
    expect = {
        ("2024-01-01", "合格"): 1, ("2024-01-01", "不合格"): 2,
        ("2024-01-02", "合格"): 1, ("2024-01-02", "不合格"): 1,
    }
    check("各组配额均满足", group_counts == expect, group_counts)
    # 1/2 合格组只能选 X1 或 X4；X2 不得出现在它没有报告的分组
    check("入选样品都在该组有对应报告",
          all(any(s["sample_no"] == r["sample_no"] and s["result"] == r["result"]
                  and s["report_time"][:10] == r["date"] for s in SHARED_REPORTS)
              for r in reports))

    section("4. 随机种子决定候选顺序，名单可复现")
    code, b1 = req("POST", API_BASE, "/samples", sample_body(42, quotas_ok))
    code, b2 = req("POST", API_BASE, "/samples", sample_body(42, quotas_ok))
    check("同发布版本同种子得同一名单",
          [r["report_no"] for r in b1["selected_reports"]]
          == [r["report_no"] for r in b2["selected_reports"]])
    code, b3 = req("POST", API_BASE, "/samples", sample_body(7, quotas_ok))
    check("不同种子均足额", b3["selected_count"] == 5)

    section("5. 无法同时满足配额：报冲突且不输出部分名单")
    # 共需 7 份 > 5 个去重样品，必然无解。
    quotas_bad = [
        {"date": "2024-01-01", "qualified": 2, "unqualified": 2},
        {"date": "2024-01-02", "qualified": 2, "unqualified": 1},
    ]
    code, body = req("POST", API_BASE, "/samples", sample_body(42, quotas_bad))
    check("配额冲突返回 409", code == 409, code)
    detail = body.get("detail", {})
    check("409 不含任何名单", "selected_reports" not in body and "selected_reports" not in detail)
    check("给出总配额与可用样品数",
          detail.get("total_quota") == 7 and detail.get("total_available_samples") == 5, detail)
    check("逐组说明配额与可用量", len(detail.get("groups", [])) == 4
          and all({"date", "result", "quota", "available_samples", "shortfall"}
                  <= set(g) for g in detail["groups"]), detail)

    section("6. 单组样品不足的冲突")
    code, body = req("POST", API_BASE, "/samples", sample_body(1, [
        {"date": "2024-01-02", "qualified": 5},
    ]))
    check("单组不足返回 409", code == 409)
    check("标注该组缺口",
          body["detail"]["groups"][0]["available_samples"] == 2
          and body["detail"]["groups"][0]["shortfall"] == 3, body["detail"])

    section("7. 重采发布后版本变化，名单来自当前发布快照")
    code, _ = req("POST", PORTAL_BASE, "/admin/switch-batch", {})
    check("门户换批到 BATCH-0002", code == 200)
    code, task = req("POST", API_BASE, "/tasks",
                     {"portal_url": PORTAL_URL, "start": RANGE_START, "end": RANGE_END})
    check("重采完成并整体替换发布", code == 201 and task["status"] == "completed"
          and task["publication"]["batch"] == "BATCH-0002", task.get("publication"))
    code, body = req("POST", API_BASE, "/samples", sample_body(42, quotas_ok))
    check("抽检反映新版本批次与任务号", code == 200
          and body["publication"]["batch"] == "BATCH-0002"
          and body["publication"]["task_id"] == task["publication"]["task_id"],
          body.get("publication"))
    check("新版本同种子名单仍足额确定", body["selected_count"] == 5
          and [r["report_no"] for r in body["selected_reports"]]
          == [r["report_no"] for r in req("POST", API_BASE, "/samples",
                                          sample_body(42, quotas_ok))[1]["selected_reports"]])

    section("8. 同一样品同组多份报告只抽一份")
    # 在独立的新区间构造：该组合格样品恰好 2 个，其中 SAM-DUP 有两份报告，
    # 配额 2 必然把两个样品都选中，可直接验证重复样品只出一份报告。
    dup_start = "2024-02-01T00:00:00Z"
    dup_end = "2024-02-02T00:00:00Z"
    req("POST", PORTAL_BASE, "/admin/seed-reports", {"reports": [
        {"sample_no": "SAM-DUP", "report_time": "2024-02-01T07:00:00Z", "result": "合格"},
        {"sample_no": "SAM-DUP", "report_time": "2024-02-01T07:30:00Z", "result": "合格"},
        {"sample_no": "SAM-OK", "report_time": "2024-02-01T07:45:00Z", "result": "合格"},
    ]})
    req("POST", API_BASE, "/tasks",
        {"portal_url": PORTAL_URL, "start": dup_start, "end": dup_end})
    code, body = req("POST", API_BASE, "/samples", {
        "portal_url": PORTAL_URL, "start": dup_start, "end": dup_end,
        "seed": 3, "quotas": [{"date": "2024-02-01", "qualified": 2}],
    })
    picked = body.get("selected_reports", [])
    dup_picked = [r for r in picked if r["sample_no"] == "SAM-DUP"]
    check("重复样品仅一份报告入选", code == 200 and len(dup_picked) == 1
          and len({r["sample_no"] for r in picked}) == 2,
          [r["report_no"] for r in picked])

    return summary()


def summary():
    print("\n" + ("全部通过 ✔" if not failures else f"失败 {len(failures)} 项: {failures}"))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
