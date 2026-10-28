#!/usr/bin/env python3
"""灵犀 V1.4 合流门禁 / 评委会打分器（纠正版）。

设计原则（2026-10-27 事故后重写）：

1. **不自己给自己发合格证**。本脚本只负责运行可复现的确定性检查；"第二方评审"
   必须由外部 Agent（claude / codex / grok / dsh 等）或人来完成，结果写到
   ``docs/evidence/redteam_review.json``。缺少或过期（reviewed_tree 不等于当前
   暂存树）时，签署一律否决。
2. **数字必须来自实测输出**，不允许硬编码测试数、通过率或准确率。
3. **拿不到证据就 fail-closed**，不猜、不填默认值。
4. 每个维度按子检查通过比例给分（部分分），不是全有或全无。

用法::

    python3 tools/ci/adversarial_jury_evaluator.py                # 完整签署模式（需要第二方评审）
    python3 tools/ci/adversarial_jury_evaluator.py --deterministic # CI 用：只跑确定性检查，不签署
    python3 tools/ci/adversarial_jury_evaluator.py --no-offline    # 跳过全量离线套件（pre-commit 用）
    python3 tools/ci/adversarial_jury_evaluator.py --emit-packet   # 输出给第二方评审的评审包

退出码：0 = 通过；1 = 否决；2 = 用法/环境错误。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCORECARD_PATH = REPO_ROOT / "docs" / "evidence" / "jury_scorecard_v14.json"
REDTEAM_PATH = REPO_ROOT / "docs" / "evidence" / "redteam_review.json"

#: 历史基线：只作为下限守卫，不代表"必须等于这个数"。
HISTORICAL_BASELINE_TESTS = 1115
SIGN_OFF_THRESHOLD = 95.0


def git(*args: str) -> str:
    res = subprocess.run(
        ["git", *args], cwd=str(REPO_ROOT), capture_output=True, text=True
    )
    return res.stdout.strip()


def run_cmd(cmd: list[str], timeout: int = 600) -> dict:
    env = dict(os.environ)
    extra = f"{REPO_ROOT / 'src'}:{REPO_ROOT / 'tests'}"
    env["PYTHONPATH"] = f"{extra}:{env['PYTHONPATH']}" if env.get("PYTHONPATH") else extra
    try:
        res = subprocess.run(
            cmd, cwd=str(REPO_ROOT), capture_output=True, text=True, env=env, timeout=timeout
        )
        return {
            "cmd": " ".join(cmd),
            "code": res.returncode,
            "out": (res.stdout or "") + (res.stderr or ""),
        }
    except subprocess.TimeoutExpired:
        return {"cmd": " ".join(cmd), "code": -1, "out": f"TIMEOUT after {timeout}s"}


def parse_ran_tests(output: str) -> int | None:
    """从 unittest 输出解析实际运行的用例数；解析不到返回 None（不猜）。"""
    matches = re.findall(r"^Ran (\d+) tests? in ", output, flags=re.MULTILINE)
    return int(matches[-1]) if matches else None


def unittest_ok(result: dict) -> bool:
    return result["code"] == 0 and "OK" in result["out"] and "FAILED" not in result["out"]


def collect(dimension: str, checks: list[dict]) -> dict:
    """checks: [{'name':..., 'ok': bool, 'detail': str}]。按通过比例给 0~20 分。"""
    passed = [c for c in checks if c["ok"]]
    score = round(20.0 * len(passed) / len(checks), 2) if checks else 0.0
    return {
        "dimension": dimension,
        "score": score,
        "checks": checks,
        "failed": [c["name"] for c in checks if not c["ok"]],
    }


def read_redteam() -> dict | None:
    if not REDTEAM_PATH.exists():
        return None
    try:
        data = json.loads(REDTEAM_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (json.JSONDecodeError, OSError):
        return None


def redteam_check() -> list[dict]:
    """第二方评审证据必须：存在、由非本 Agent 执行、评审的树等于当前暂存树、结论为 pass。"""
    data = read_redteam()
    if data is None:
        return [
            {
                "name": "第二方评审证据存在",
                "ok": False,
                "detail": f"缺少 {REDTEAM_PATH.relative_to(REPO_ROOT)}；不得由本 Agent 自签，"
                          "须由外部 Agent 或人完成评审后写入",
            }
        ]
    current_tree = git("write-tree")
    checks = [
        {
            "name": "评审者非本 Agent 自评",
            "ok": str(data.get("reviewer_kind", "")).lower() in {"external_cli", "human"},
            "detail": f"reviewer_kind={data.get('reviewer_kind')!r}, reviewer={data.get('reviewer')!r}",
        },
        {
            "name": "评审对象与当前暂存树一致",
            "ok": bool(current_tree) and data.get("reviewed_tree") == current_tree,
            "detail": f"reviewed_tree={data.get('reviewed_tree')!r}, current_tree={current_tree!r}",
        },
        {
            "name": "评审结论为通过",
            "ok": str(data.get("verdict", "")).lower() == "pass",
            "detail": f"verdict={data.get('verdict')!r}",
        },
        {
            "name": "评审意见可追溯（至少一条 finding 或明确写 no_findings）",
            "ok": bool(data.get("findings")) or data.get("no_findings") is True,
            "detail": f"findings={len(data.get('findings') or [])}",
        },
    ]
    return checks


def build_packet(offline_result: dict | None) -> str:
    """给第二方评审用的评审包：变更清单 + 门禁实测结果，审阅者据此独立判断。"""
    lines = [
        "# 灵犀 V1.4 第二方评审包",
        f"- 分支: {git('rev-parse', '--abbrev-ref', 'HEAD')}",
        f"- HEAD: {git('rev-parse', 'HEAD')}",
        f"- 暂存树: {git('write-tree')}",
        "",
        "## 变更文件",
        git("diff", "--cached", "--stat") or "(暂存区为空；请先 git add)",
        "",
        "## 工作区变更（未暂存）",
        git("diff", "--stat") or "(无)",
    ]
    if offline_result:
        ran = parse_ran_tests(offline_result["out"])
        lines += [
            "",
            "## 确定性门禁实测",
            f"- 命令: {offline_result['cmd']}",
            f"- 退出码: {offline_result['code']}",
            f"- 解析用例数: {ran}",
        ]
    lines += [
        "",
        "## 评审要求",
        "只读审查：指出具体缺陷、复现命令、严重级别；禁止修改文件。",
        "完成后把结论写入 docs/evidence/redteam_review.json，schema 见 "
        "docs/agents/adversarial-review-protocol.md。",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="灵犀 V1.4 合流门禁 / 评委会打分器")
    parser.add_argument("--deterministic", action="store_true", help="只跑确定性检查，不要求第二方评审、不签署")
    parser.add_argument("--no-offline", action="store_true", help="跳过全量离线套件（CI 已跑过时用）")
    parser.add_argument("--emit-packet", action="store_true", help="输出第二方评审包后退出")
    args = parser.parse_args()

    if not (REPO_ROOT / ".git").exists():
        print("错误：不在仓库根目录或不是 git 仓库", file=sys.stderr)
        return 2

    start = time.time()
    dimensions: list[dict] = []
    veto: list[str] = []

    # 维度 1：适配器契约
    r1 = run_cmd([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_adapters_*.py"])
    ran1 = parse_ran_tests(r1["out"])
    dimensions.append(collect("1_architecture_and_contract", [
        {"name": "适配器单测退出码 0", "ok": r1["code"] == 0, "detail": r1["cmd"]},
        {"name": "适配器单测全通过", "ok": unittest_ok(r1), "detail": f"ran={ran1}"},
        {"name": "用例数已解析（≥1）", "ok": (ran1 or 0) >= 1, "detail": f"ran={ran1}"},
    ]))

    # 维度 2：并发/死锁
    r2 = run_cmd([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_adversarial_mapping_race.py"])
    ran2 = parse_ran_tests(r2["out"])
    dimensions.append(collect("2_concurrency_and_deadlock", [
        {"name": "并发对抗测试全通过", "ok": unittest_ok(r2), "detail": r2["cmd"]},
        {"name": "用例数已解析（≥1）", "ok": (ran2 or 0) >= 1, "detail": f"ran={ran2}"},
    ]))

    # 维度 3：边界/对抗
    r3 = {}
    for pattern in ("test_adversarial_geometry.py", "test_adversarial_inputs.py", "test_adversarial_battles.py"):
        r3[pattern] = run_cmd([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", pattern])
    dimensions.append(collect("3_boundary_and_fail_closed", [
        {"name": f"{p} 全通过", "ok": unittest_ok(r), "detail": r["cmd"]} for p, r in r3.items()
    ]))

    # 维度 4：意图评测 + E2E 冒烟
    r4a = run_cmd([sys.executable, "-m", "unittest", "discover", "-s", "tests/evals", "-p", "test_*.py"])
    r4b = run_cmd([sys.executable, "tools/harness/e2e_smoke_runner.py"], timeout=180)
    action_ratios = re.findall(r"动作命中: \d+ \((\d+(?:\.\d+)?)%\)", r4a["out"])
    param_ratios = re.findall(r"参数命中: \d+ \((\d+(?:\.\d+)?)%\)", r4a["out"])
    action_ratio = float(action_ratios[-1]) if action_ratios else None
    param_ratio = float(param_ratios[-1]) if param_ratios else None
    dimensions.append(collect("4_intent_and_simulation", [
        {"name": "意图评测套件通过", "ok": unittest_ok(r4a), "detail": r4a["cmd"]},
        {"name": "动作准确率 ≥95%", "ok": action_ratio is not None and action_ratio >= 95.0, "detail": f"实测 {action_ratio}%"},
        {"name": "参数准确率 ≥98%", "ok": param_ratio is not None and param_ratio >= 98.0, "detail": f"实测 {param_ratio}%"},
        {"name": "E2E 冒烟通过（本地 Mock，不代表真机）",
         "ok": r4b["code"] == 0 and "[E2E Smoke PASS]" in r4b["out"], "detail": r4b["cmd"]},
    ]))

    # 维度 5：编译 / 空白 / 全量离线回归
    r5a = run_cmd([sys.executable, "-m", "compileall", "-q", "src", "tests", "tools"])
    r5b = run_cmd(["git", "diff", "--check"])
    offline_result = None
    ran5 = None
    dim5_checks = [
        {"name": "compileall 无错误", "ok": r5a["code"] == 0, "detail": r5a["cmd"]},
        {"name": "git diff --check 无空白错误", "ok": r5b["code"] == 0, "detail": r5b["cmd"]},
    ]
    if args.no_offline:
        dim5_checks.append({
            "name": "全量离线套件（本次跳过）",
            "ok": True,
            "detail": "SKIPPED：调用方须自行证明已运行 bash tools/ci/offline_tests.sh",
        })
    else:
        r5c = run_cmd(["bash", "tools/ci/offline_tests.sh"], timeout=900)
        offline_result = r5c
        ran5 = parse_ran_tests(r5c["out"])
        dim5_checks += [
            {"name": "全量离线套件通过",
             "ok": r5c["code"] == 0 and unittest_ok(r5c), "detail": r5c["cmd"]},
            {"name": f"用例数不低于历史基线 {HISTORICAL_BASELINE_TESTS}",
             "ok": ran5 is not None and ran5 >= HISTORICAL_BASELINE_TESTS, "detail": f"实测 ran={ran5}"},
        ]
    dimensions.append(collect("5_code_cleanliness_and_regression", dim5_checks))

    # 第二方评审
    second_party = redteam_check()
    if not args.deterministic:
        for c in second_party:
            if not c["ok"]:
                veto.append(f"第二方评审不成立：{c['name']}（{c['detail']}）")
    else:
        veto.append("deterministic 模式：未要求第二方评审，本成绩单不可用于签署")

    for dim in dimensions:
        for name in dim["failed"]:
            veto.append(f"{dim['dimension']} 子检查失败：{name}")

    total = round(sum(d["score"] for d in dimensions), 2)
    sign_off = total >= SIGN_OFF_THRESHOLD and not veto and not args.deterministic

    if args.emit_packet:
        print(build_packet(offline_result))
        return 0

    print("=" * 70)
    print("⚖️  灵犀 V1.4 合流门禁评分（纠正版：数据来自实测，签署需第二方评审）")
    print("=" * 70)
    for dim in dimensions:
        print(f"\n[{dim['dimension']}] {dim['score']:.2f} / 20.0")
        for c in dim["checks"]:
            print(f"  {'✅' if c['ok'] else '❌'} {c['name']}  ({c['detail']})")
    print("\n[第二方评审]")
    for c in second_party:
        print(f"  {'✅' if c['ok'] else '❌'} {c['name']}  ({c['detail']})")
    print("\n" + "=" * 70)
    print(f"总分: {total:.2f} / 100.0（签署阈值 {SIGN_OFF_THRESHOLD}）")
    print(f"签署: {'通过 SIGN-OFF' if sign_off else '否决 REJECTED'}")
    print("=" * 70)

    evidence = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "mode": "deterministic" if args.deterministic else "sign_off",
        "git_head": git("rev-parse", "HEAD"),
        "staged_tree": git("write-tree"),
        "total_score": total,
        "sign_off_threshold": SIGN_OFF_THRESHOLD,
        "sign_off_passed": sign_off,
        "deterministic_only": args.deterministic,
        "dimensions": dimensions,
        "second_party_review": second_party,
        "veto_failures": veto,
        "observed": {
            "offline_tests_ran": ran5,
            "baseline_guard": HISTORICAL_BASELINE_TESTS,
            "eval_action_accuracy_pct": action_ratio,
            "eval_param_accuracy_pct": param_ratio,
        },
        "note": "数值均由命令输出解析；第二方评审文件：docs/evidence/redteam_review.json",
        "elapsed_seconds": round(time.time() - start, 2),
    }
    SCORECARD_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCORECARD_PATH.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"成绩单已写入: {SCORECARD_PATH.relative_to(REPO_ROOT)}")

    return 0 if sign_off else 1


if __name__ == "__main__":
    sys.exit(main())
