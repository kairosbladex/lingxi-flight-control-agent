#!/usr/bin/env python3
"""跨仓隔离联调：规划 Agent → 交接契约 → 飞控 Agent → 假 Java/HTTP。

只在本机 127.0.0.1 的临时端口上跑，不连接 MQTT、真实无人机或共享 test/uat/prod。
假 Java 只返回**合成**快照与围栏结论，不冒充任何真实平台能力。

用法::

    python3 tools/ci/cross_repo_isolation.py [--agent-src PATH] [--json]

退出码 0 表示全部场景通过；任何失败返回 1。
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
DEFAULT_AGENT_SRC = Path("<local-path>/PycharmProjects/lingxi-agent")
ACTOR = "emp-1"
PROJECT = "proj-1"
SITE = "site-1"
HANGAR = "hangar-1"
DRONE = "drone-1"
TASK = "task-1"
BATCH = "batch-1"
CONTEXT_VERSION = 3


def _hex(seed: str) -> str:
    import hashlib
    return hashlib.sha256(seed.encode()).hexdigest()


class FakeJava:
    """合成平台：快照 + 围栏 + 指令受理。记录每个请求，便于断言“没有下发”。"""

    def __init__(self) -> None:
        self.requests: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        self.server: ThreadingHTTPServer | None = None
        self.thread: threading.Thread | None = None
        self.port = 0

    def snapshot(self, now: float) -> dict:
        actions = ["read_state", "hover"]
        return {"code": 200, "success": True, "data": {
            "schema_version": "lingxi-platform-snapshot/v1",
            "snapshot_version": _hex("snapshot"),
            "generated_at": now, "expires_at": now - 1 + 5,
            "actor_employee_id": ACTOR, "project_id": PROJECT, "site_id": SITE,
            "hangar_id": HANGAR, "drone_id": DRONE, "task_id": TASK,
            "control_owner_employee_id": ACTOR, "control_owner_updated_at": now - 30,
            "control_lease_expires_at": now + 270, "control_owner_version": _hex("owner"),
            "profile": "DJI_HANGAR",
            "telemetry": {"sampled_at": now - 1, "received_at": now - 0.5,
                          "source": "java-udp-cache", "sequence": None, "version": _hex("telemetry")},
            "state": {"online": True, "airborne": True, "longitude": 118.1, "latitude": 34.2,
                      "altitude_m": 50.0, "height_m": None, "yaw_deg": 90.0,
                      "battery_percent": 88.0, "flight_status": "FLYING", "route_status": None},
            "capabilities": {action: action in actions for action in
                             ["read_state", "hover", "point_flight", "route_pause"]}
                            | {"UAV_MOVE_DISTANCE": False},
            "permissions": actions,
            "action_availability": {action: {"available": action in actions,
                                             "reasons": [] if action in actions else ["CAPABILITY_UNAVAILABLE"]}
                                    for action in ["read_state", "hover", "point_flight", "route_pause"]},
            "contract": {"schema_version": "lingxi-platform-contract/v1", "unavailable": {
                "route_status": "ROUTE_STATE_SOURCE_UNAVAILABLE",
                "target_binding": "STATIC_ANNOTATION_SOURCE_UNAVAILABLE",
                "algorithm_lifecycle": "ALGORITHM_LIFECYCLE_UNAVAILABLE",
                "mapping_transaction": "MAPPING_TRANSACTION_UNAVAILABLE",
            }},
        }}

    def record(self, method: str, path: str) -> None:
        with self.lock:
            self.requests.append((method, path.split("?")[0]))

    def count(self, prefix: str) -> int:
        with self.lock:
            return sum(1 for _, path in self.requests if path.startswith(prefix))

    def start(self) -> None:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args) -> None:  # 不打印访问日志，避免噪音
                return

            def _send(self, payload: dict, status: int = 200) -> None:
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                fake.record("GET", self.path)
                if self.path.startswith("/api/lingxi/agent/platform/snapshot"):
                    self._send(fake.snapshot(time.time()))
                    return
                self._send({"code": 404, "message": "NOT_FOUND"}, 404)

            def do_POST(self) -> None:  # noqa: N802
                fake.record("POST", self.path)
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    self._send({"code": 400, "message": "BAD_JSON"}, 400)
                    return
                if self.path.startswith("/api/electronicFence/path-route/with-clean"):
                    points = body.get("points") or []
                    self._send({"code": 200, "success": True, "data": {
                        "projectId": self.headers.get("projectId"),
                        "operatorId": ACTOR,
                        "snapshot": points,
                        "invalid": [],
                        "segment": [{"status": "ok", "converge": "ln_outside"}
                                    for _ in range(max(0, len(points) - 1))],
                    }})
                    return
                if self.path.startswith("/screen/uav/operation/control"):
                    self._send({"code": 200, "success": True, "data": {"accepted": True}})
                    return
                self._send({"code": 404, "message": "NOT_FOUND"}, 404)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
        self.server = None
        self.thread = None


class Recorder:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def check(self, name: str, ok: bool, detail: str = "") -> bool:
        self.rows.append({"scenario": name, "ok": bool(ok), "detail": detail})
        return bool(ok)


def binding_payload(snapshot_version: str) -> dict:
    return {"actor_id": ACTOR, "project_id": PROJECT, "site_id": SITE, "hangar_id": HANGAR,
            "drone_id": DRONE, "task_id": TASK, "task_batch_id": BATCH,
            "context_version": CONTEXT_VERSION, "snapshot_version": snapshot_version}


def point(target_id: str, name: str, **extra) -> dict:
    value = {"target_id": target_id, "name": name, "geometry_type": "point",
             "longitude": 118.1, "latitude": 34.2, "altitude_m": 50.0,
             "version": "v1", "source": "static-annotation", "project_id": PROJECT}
    value.update(extra)
    return value


def run(agent_src: Path) -> tuple[list[dict], bool]:
    sys.path.insert(0, str(REPO / "src"))
    sys.path.insert(0, str(agent_src))
    os.environ.setdefault("LINGXI_FLIGHT_CONTROL_SRC", str(REPO / "src"))

    from lingxi_flight_control import handoff_contract as contract
    from lingxi_flight_control.full_control import FlightControlActionRequest, RuntimeBinding
    from lingxi_flight_control.ktd_path import KtdPathChecker
    from lingxi_flight_control.ktd_platform import (
        KtdHttpTransport, KtdPlatformExecutor, KtdPlatformError, SqliteCommandJournal,
    )
    from lingxi_flight_control.ktd_snapshot import KtdSnapshotProvider

    # 按文件加载规划侧模块：``app/__init__.py`` 会拉起 Flask 等运行依赖，
    # 而交接产出方本身只依赖标准库 + 共享契约，隔离联调不需要整套 Web 栈。
    import importlib.util
    module_path = agent_src / "app" / "runtime" / "flight_handoff.py"
    spec = importlib.util.spec_from_file_location("lingxi_agent_flight_handoff", module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载 {module_path}")
    flight_handoff = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(flight_handoff)

    recorder = Recorder()
    fake = FakeJava()
    baseline_threads = threading.active_count()
    fake.start()
    base_url = f"http://127.0.0.1:{fake.port}/api"
    binding = RuntimeBinding(ACTOR, PROJECT, HANGAR, DRONE, BATCH, CONTEXT_VERSION)
    # 仅回环地址放开明文 HTTP；真实平台仍必须 https。
    transport = KtdHttpTransport(base_url, token="fixture-token", actor_id=ACTOR,
                                 allow_insecure_http=True)
    provider = KtdSnapshotProvider(binding, site_id=SITE, task_id=TASK, transport=transport)

    try:
        # --- 读取权威快照（真实 HTTP，回环） -------------------------------
        snapshot = provider()
        recorder.check("S0 权威快照可读且带显式不可用声明",
                       snapshot.platform_unavailable.get("route_status") == "ROUTE_STATE_SOURCE_UNAVAILABLE",
                       f"unavailable={sorted(snapshot.platform_unavailable)}")

        # --- S1 点目标进入飞控上下文 ---------------------------------------
        payload = flight_handoff.build_flight_control(
            binding=binding_payload(snapshot.version), target=point("t-1", "1号水塔"),
            intent="point_flight", trace_id="trace-1", command_id="cmd-1", idempotency_key="idem-1")
        envelope = contract.validate_envelope(payload, expected_binding=binding, site_id=SITE, task_id=TASK)
        request = contract.to_action_request(envelope)
        recorder.check("S1 点目标坐标进入飞控上下文",
                       str(request.action) == "point_flight"
                       and request.parameters == {"longitude": 118.1, "latitude": 34.2, "altitude_m": 50.0},
                       f"action={request.action}")
        contract.verify_snapshot(envelope, snapshot)
        recorder.check("S1b 交接信封与权威快照版本一致", True, f"snapshot={snapshot.version[:8]}…")

        # --- S2 线/面/体/混合拒绝 ------------------------------------------
        def rejected(builder) -> str:
            try:
                builder()
            except contract.HandoffError as error:
                return error.code
            return "NO_ERROR"

        line = {"target_id": "t-line", "name": "B河道", "geometry_type": "line",
                "version": "v1", "source": "static-annotation", "project_id": PROJECT}
        codes = {
            "line→flight": rejected(lambda: flight_handoff.build_flight_control(
                binding=binding_payload(snapshot.version), target=line, intent="point_flight",
                trace_id="t", command_id="c", idempotency_key="k")),
            "polygon→flight": rejected(lambda: flight_handoff.build_flight_control(
                binding=binding_payload(snapshot.version), target={**point("t-p", "C片区"),
                "geometry_type": "polygon"}, intent="point_orbit", radius_m=30,
                trace_id="t", command_id="c", idempotency_key="k")),
            "volume": rejected(lambda: flight_handoff.build_task_dispatch(
                binding=binding_payload(snapshot.version), targets=[point("t-v", "楼", geometry_type="volume")],
                task={"task_type": "inspection"}, draft={"confirmed": True, "dispatch_id": "d-1"},
                trace_id="t", command_id="c", idempotency_key="k")),
            "mixed": rejected(lambda: flight_handoff.build_task_dispatch(
                binding=binding_payload(snapshot.version), targets=[point("t-1", "1号"), line],
                task={"task_type": "inspection"}, draft={"confirmed": True, "dispatch_id": "d-1"},
                trace_id="t", command_id="c", idempotency_key="k")),
        }
        recorder.check("S2 线/面/体/混合目标全部拒绝",
                       codes["line→flight"] == "HANDOFF_TARGET_GEOMETRY_UNSUPPORTED"
                       and codes["polygon→flight"] == "HANDOFF_TARGET_GEOMETRY_UNSUPPORTED"
                       and codes["volume"] == "HANDOFF_TARGET_GEOMETRY_UNSUPPORTED"
                       and codes["mixed"] == "HANDOFF_MIXED_TARGET_UNSUPPORTED",
                       json.dumps(codes, ensure_ascii=False))

        # --- S3 多点串联保序 ------------------------------------------------
        dispatch = flight_handoff.build_task_dispatch(
            binding=binding_payload(snapshot.version),
            targets=[point("t-3", "3号"), point("t-1", "1号"), point("t-2", "2号")],
            task={"task_type": "inspection"},
            draft={"confirmed": True, "dispatch_id": "dispatch-1", "revision": 1},
            trace_id="trace-2", command_id="cmd-2", idempotency_key="idem-2")
        order = [item["target_id"] for item in dispatch["targets"]]
        recorder.check("S3 多点串联保留用户顺序", order == ["t-3", "t-1", "t-2"], str(order))

        # --- S4 未确认草稿不下发 --------------------------------------------
        draft_code = rejected(lambda: flight_handoff.build_task_dispatch(
            binding=binding_payload(snapshot.version), targets=[point("t-1", "1号")],
            task={"task_type": "inspection"}, draft={"confirmed": False, "revision": 1},
            trace_id="t", command_id="c", idempotency_key="k"))
        recorder.check("S4 未确认草稿不产出可下发任务",
                       draft_code == "HANDOFF_DRAFT_NOT_DISPATCHABLE", draft_code)

        # --- S5 不重复创建第二个任务 ----------------------------------------
        recorder.check("S5 飞行控制交接不创建第二个任务",
                       envelope.creates_task() is False and dispatch and True,
                       "creates_task=False")

        # --- S6 stale context 是业务 409 ------------------------------------
        stale = flight_handoff.build_flight_control(
            binding={**binding_payload(snapshot.version), "context_version": CONTEXT_VERSION - 1},
            target=point("t-1", "1号水塔"), intent="point_flight",
            trace_id="t", command_id="c", idempotency_key="k")
        try:
            contract.validate_envelope(stale, expected_binding=binding, site_id=SITE, task_id=TASK)
            stale_code, stale_status = "NO_ERROR", 0
        except contract.HandoffError as error:
            stale_code, stale_status = error.code, error.http_status
        recorder.check("S6 旧上下文按 409 拒绝", stale_code == "STALE_CONTEXT" and stale_status == 409,
                       f"{stale_code}/{stale_status}")

        # --- S7 幂等键 ------------------------------------------------------
        ledger = contract.HandoffLedger()
        first = ledger.admit(envelope)
        second = ledger.admit(contract.validate_envelope(
            {**payload, "trace_id": "trace-9"}, expected_binding=binding, site_id=SITE, task_id=TASK))
        conflict = rejected(lambda: ledger.admit(contract.validate_envelope(
            {**payload, "targets": [{**point("t-1", "1号水塔", altitude_m=60.0), "order": 1}],
             "action": {"name": "point_flight", "parameters": {"longitude": 118.1, "latitude": 34.2,
                                                              "altitude_m": 60.0}}},
            expected_binding=binding, site_id=SITE, task_id=TASK)))
        recorder.check("S7 同键同体幂等、同键异体冲突",
                       (first, second, conflict) == ("new", "duplicate", "HANDOFF_IDEMPOTENCY_CONFLICT"),
                       f"{first}/{second}/{conflict}")

        # --- S8 Java 声明的能力缺口优先于通用拒绝，且零下发 -----------------
        journal_path = Path("/tmp") / f"v14-isolation-{os.getpid()}.db"
        journal = SqliteCommandJournal(journal_path)
        try:
            executor = KtdPlatformExecutor(
                binding, site_id=SITE, task_id=TASK, transport=transport,
                snapshot_provider=provider, enabled=True, journal=journal)
            hover = executor.preview(FlightControlActionRequest("cmd-h", "idem-h", "hover", {}, CONTEXT_VERSION))
            writes_before = fake.count("/screen/uav/operation/control")
            point_receipt = executor.preview(FlightControlActionRequest(
                "cmd-p", "idem-p", "point_flight",
                {"longitude": 118.1, "latitude": 34.2, "altitude_m": 50.0}, CONTEXT_VERSION))
        finally:
            journal_path.unlink(missing_ok=True)
        recorder.check("S8 hover 返回平台声明的 typed 缺口",
                       hover.error_code == "ROUTE_STATE_SOURCE_UNAVAILABLE", str(hover.error_code))
        recorder.check("S8b 指点动作未被能力清单放行",
                       point_receipt.error_code in {"ACTION_NOT_PERMITTED", "ACTION_UNAVAILABLE",
                                                    "STATIC_ANNOTATION_SOURCE_UNAVAILABLE"},
                       str(point_receipt.error_code))
        recorder.check("S8c 预览阶段零飞控下发", writes_before == 0, f"writes={writes_before}")

        # --- S9 围栏检查走真实回环 HTTP -------------------------------------
        checker = KtdPathChecker(transport)
        fence = checker(binding, "move_forward", {"distance_m": 10},
                        {"longitude": 118.1, "latitude": 34.2, "altitude_m": 50.0, "yaw_deg": 90.0})
        recorder.check("S9 围栏结论经回环 HTTP 校验", fence.status == "clear", f"{fence.status}/{fence.reason}")

        # --- S10 交付阶段：受理/提交/未知都不是设备完成 ---------------------
        stages = {status: contract.delivery_stage(status).value
                  for status in ("accepted", "submitted", "someday")}
        recorder.check("S10 unknown 不显示为成功且都不是设备效果",
                       stages["accepted"] == "accepted" and stages["submitted"] == "submitted"
                       and stages["someday"] == "unknown"
                       and not contract.device_effect_verified("unknown")
                       and not contract.device_effect_verified("accepted"),
                       json.dumps(stages, ensure_ascii=False))
    except (KtdPlatformError, contract.HandoffError, ValueError, OSError) as error:
        recorder.check("运行期异常", False, f"{type(error).__name__}: {error}")
    finally:
        fake.stop()

    # --- S11 端口关闭与线程回收 ---------------------------------------------
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        closed = probe.connect_ex(("127.0.0.1", fake.port)) != 0
    recorder.check("S11 服务结束后端口已关闭", closed, f"port={fake.port}")
    recorder.check("S11b 无遗留监听线程", threading.active_count() <= baseline_threads,
                   f"threads={threading.active_count()}/{baseline_threads}")
    recorder.check("S11c 全程未触碰真实平台写接口",
                   fake.count("/screen/uav/operation/control") == 0,
                   f"writes={fake.count('/screen/uav/operation/control')}")

    ok = all(row["ok"] for row in recorder.rows)
    return recorder.rows, ok


def main() -> int:
    parser = argparse.ArgumentParser(description="V1.4 跨仓隔离联调")
    parser.add_argument("--agent-src", default=str(DEFAULT_AGENT_SRC))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    agent_src = Path(args.agent_src)
    if not (agent_src / "app" / "runtime" / "flight_handoff.py").is_file():
        print(f"规划 Agent 源码不可用：{agent_src}", file=sys.stderr)
        return 2
    rows, ok = run_captured(agent_src)
    if args.json:
        print(json.dumps({"ok": ok[0], "audit_events": ok[1], "scenarios": rows},
                         ensure_ascii=False, indent=2))
    else:
        for row in rows:
            print(f"[{'PASS' if row['ok'] else 'FAIL'}] {row['scenario']}"
                  + (f" — {row['detail']}" if row["detail"] else ""))
        print(f"\n{'全部通过' if ok[0] else '存在失败'}：{sum(1 for r in rows if r['ok'])}/{len(rows)}"
              f"（审计事件 {ok[1]} 条）")
    return 0 if ok[0] else 1


def run_captured(agent_src: Path) -> tuple[list[dict], tuple[bool, int]]:
    """跑联调并收集 stdout 上的结构化审计行，避免污染 --json 输出。"""
    import contextlib
    import io

    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        rows, ok = run(agent_src)
    events = sum(1 for line in buffer.getvalue().splitlines() if line.strip().startswith("{"))
    return rows, (ok, events)


if __name__ == "__main__":
    raise SystemExit(main())
