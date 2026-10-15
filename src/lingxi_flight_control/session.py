"""会话检查点持久化：让一次任务能跨请求、跨进程续跑。

问题：确认握手原先依赖「同一个进程里的同一个 `AgentCheckpoint` 对象」——HTTP
请求之间只要换一个进程，待确认的动作就丢了，或者反过来被重复执行。

本模块把检查点变成**可持久化、可校验、可幂等重放**的会话记录：

- `SessionRecord`   会话快照（检查点 + 时间戳 + 版本号 + 有界请求日志）；
- `CheckpointStore` 存储协议，内存与文件两个实现；
- `SessionRunner`   把「加载 -> 驱动 `BoundedAgentLoop` -> 保存」串起来，
                    并在同一次调用内完成确认握手的续跑。

安全边界**不在**这一层：真正决定「这次工具调用是否被授权」的仍然是
`BoundedAgentLoop` 对 user_input + context + runtime_context + tool call 的绑定校验。
本模块只负责「同一份检查点被原样喂回去」，不新增任何绕过路径。
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import copy
import json
import math
import os
import re
import tempfile
import time
from typing import Any, Protocol, runtime_checkable

from .models import AgentCheckpoint, ExecutionContext, LoopStatus

#: 会话文件/记录的 schema 版本。不匹配一律按篡改处理，绝不「尽力解析」。
SESSION_SCHEMA_VERSION = 1

#: 默认 TTL：30 分钟。一次确认握手不会等这么久，但跨请求续跑要留足余量。
DEFAULT_TTL_SEC = 1800.0

#: 请求日志上限。滚动保留最后 N 条，避免会话文件无界增长。
MAX_REQUEST_LOG = 50

#: session_id 白名单：文件存储把它当文件名用，必须结构上排除路径穿越。
_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

#: `SessionRunner.handle` 返回字典的稳定键集合（服务层与 CLI 的契约）。
RESPONSE_KEYS = (
    "session_id", "status", "message", "step_count", "max_steps",
    "executed", "tool_call", "error", "checkpoint_revision", "request_id",
)


class SessionError(ValueError):
    """会话层错误基类。"""


class SessionNotFound(SessionError):
    """会话不存在。"""


class SessionExpired(SessionError):
    """会话已过 TTL。"""


class SessionTampered(SessionError):
    """会话内容不合法：schema 版本不符、JSON 不可解析、运行时绑定被改写等。"""


def validate_session_id(session_id: Any) -> str:
    """严格校验 session_id；它是文件名的一部分，路径穿越必须在这里被挡住。"""
    if not isinstance(session_id, str) or not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise SessionError(
            f"invalid session_id {session_id!r}: must match {_SESSION_ID_PATTERN.pattern}")
    return session_id


def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SessionError(f"{name} must be a positive finite number")
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise SessionError(f"{name} must be a positive finite number")
    return number


def _timestamp(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SessionTampered(f"{name} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise SessionTampered(f"{name} must be finite")
    return number


def _request_log(value: Any) -> tuple[dict[str, Any], ...]:
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise SessionTampered("request_log must be an array")
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise SessionTampered(f"request_log #{index} must be an object")
        unknown = sorted(set(item) - {"request_id", "response"})
        if unknown:
            raise SessionTampered(f"request_log #{index} has unknown fields: {unknown}")
        request_id = item.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise SessionTampered(f"request_log #{index} request_id must be a non-empty string")
        response = item.get("response")
        if response is not None and not isinstance(response, Mapping):
            raise SessionTampered(f"request_log #{index} response must be an object or null")
        entries.append({
            "request_id": request_id,
            "response": None if response is None else copy.deepcopy(dict(response)),
        })
    return tuple(entries)


def _log_entry(request: Mapping[str, Any] | None,
               response: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """把一次请求/响应压成一条日志；没有 request_id 就不记（无法按幂等键检索）。"""
    if request is None:
        return None
    if not isinstance(request, Mapping):
        raise SessionError("request must be an object")
    request_id = request.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        raise SessionError("request must carry a non-empty string request_id")
    if response is not None and not isinstance(response, Mapping):
        raise SessionError("response must be an object or null")
    return {
        "request_id": request_id,
        "response": None if response is None else copy.deepcopy(dict(response)),
    }


@dataclass(frozen=True)
class SessionRecord:
    """一个会话的持久化快照；`to_dict()`/`from_dict()` 保证 JSON 往返。"""

    session_id: str
    checkpoint: AgentCheckpoint
    created_at: float
    updated_at: float
    expires_at: float
    revision: int
    request_log: tuple[dict[str, Any], ...] = ()
    schema_version: int = SESSION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "session_id": self.session_id,
            "checkpoint": self.checkpoint.to_dict(),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expires_at": self.expires_at,
            "revision": self.revision,
            "request_log": [copy.deepcopy(dict(entry)) for entry in self.request_log],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SessionRecord":
        """从 JSON 对象恢复会话；任何结构问题都以 `SessionTampered` 失败（fail closed）。"""
        if not isinstance(value, Mapping):
            raise SessionTampered("session record must be a JSON object")
        if value.get("schema_version") != SESSION_SCHEMA_VERSION:
            raise SessionTampered(
                f"session schema_version must be {SESSION_SCHEMA_VERSION}, "
                f"got {value.get('schema_version')!r}")
        session_id = value.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise SessionTampered("session_id must be a non-empty string")
        raw_checkpoint = value.get("checkpoint")
        if not isinstance(raw_checkpoint, Mapping):
            raise SessionTampered("session checkpoint must be an object")
        # 运行时绑定单独先验一遍：它决定「这个检查点还属不属于当前 Runtime」，
        # 形状不对就不能靠 `AgentCheckpoint` 的属性访问碰运气。
        runtime_context = raw_checkpoint.get("runtime_context")
        if runtime_context is not None:
            if not isinstance(runtime_context, Mapping):
                raise SessionTampered("checkpoint runtime_context must be an object")
            try:
                ExecutionContext.from_mapping(runtime_context)
            except ValueError as exc:
                raise SessionTampered(f"checkpoint runtime_context is invalid: {exc}") from exc
        try:
            checkpoint = AgentCheckpoint.from_dict(raw_checkpoint)
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionTampered(f"checkpoint cannot be restored: {exc}") from exc
        revision = value.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
            raise SessionTampered("revision must be a non-negative integer")
        return cls(
            session_id=session_id,
            checkpoint=checkpoint,
            created_at=_timestamp(value.get("created_at"), "created_at"),
            updated_at=_timestamp(value.get("updated_at"), "updated_at"),
            expires_at=_timestamp(value.get("expires_at"), "expires_at"),
            revision=revision,
            request_log=_request_log(value.get("request_log")),
            schema_version=SESSION_SCHEMA_VERSION,
        )

    def response_for(self, request_id: str | None) -> dict[str, Any] | None:
        """按幂等键取回已存响应；同键多次写入时以后写的为准。"""
        if not request_id:
            return None
        found: dict[str, Any] | None = None
        for entry in self.request_log:
            if entry.get("request_id") == request_id and entry.get("response") is not None:
                found = copy.deepcopy(entry["response"])
        return found

    def is_expired(self, now: float) -> bool:
        return float(now) >= self.expires_at


@runtime_checkable
class CheckpointStore(Protocol):
    """会话检查点存储。两个实现（内存/文件）语义必须一致。"""

    def save(self, session_id: str, checkpoint: AgentCheckpoint, *,
             request: Mapping[str, Any] | None = None,
             response: Mapping[str, Any] | None = None,
             ttl_sec: float | None = None) -> SessionRecord:
        """upsert 一个会话：`revision` 自增 1，`created_at` 保持不变。"""
        ...

    def load(self, session_id: str) -> SessionRecord:
        """读取会话；不存在 -> `SessionNotFound`，过期 -> `SessionExpired`。"""
        ...

    def delete(self, session_id: str) -> None:
        """删除会话；不存在时静默返回。"""
        ...

    def list_sessions(self) -> tuple[str, ...]:
        """列出全部会话 id（含已过期条目），顺序稳定。"""
        ...


class MemoryCheckpointStore:
    """进程内存存储；`clock` 可注入，便于测试 TTL 边界。

    保存与读取都会深拷贝检查点：存储不再与循环持有的可变对象共享状态，
    这样「同一进程里碰巧活着同一个对象」不再是隐藏依赖。
    """

    def __init__(self, *, clock: Callable[[], float] | None = None,
                 ttl_sec: float = DEFAULT_TTL_SEC,
                 max_request_log: int = MAX_REQUEST_LOG):
        if isinstance(max_request_log, bool) or not isinstance(max_request_log, int) or max_request_log <= 0:
            raise SessionError("max_request_log must be a positive integer")
        self._clock = clock or time.time
        self._ttl_sec = _positive_number(ttl_sec, "ttl_sec")
        self._max_request_log = max_request_log
        self._records: dict[str, SessionRecord] = {}

    @property
    def ttl_sec(self) -> float:
        return self._ttl_sec

    def save(self, session_id: str, checkpoint: AgentCheckpoint, *,
             request: Mapping[str, Any] | None = None,
             response: Mapping[str, Any] | None = None,
             ttl_sec: float | None = None) -> SessionRecord:
        validate_session_id(session_id)
        if not isinstance(checkpoint, AgentCheckpoint):
            raise SessionError("checkpoint must be an AgentCheckpoint")
        now = float(self._clock())
        ttl = self._ttl_sec if ttl_sec is None else _positive_number(ttl_sec, "ttl_sec")
        existing = self._records.get(session_id)
        if existing is None:
            created_at, revision, log = now, 1, ()
        else:
            created_at, revision, log = existing.created_at, existing.revision + 1, existing.request_log
        entry = _log_entry(request, response)
        if entry is not None:
            log = (log + (entry,))[-self._max_request_log:]
        record = SessionRecord(
            session_id=session_id,
            checkpoint=copy.deepcopy(checkpoint),
            created_at=created_at,
            updated_at=now,
            expires_at=now + ttl,
            revision=revision,
            request_log=log,
            schema_version=SESSION_SCHEMA_VERSION,
        )
        self._records[session_id] = record
        return copy.deepcopy(record)

    def load(self, session_id: str) -> SessionRecord:
        validate_session_id(session_id)
        record = self._records.get(session_id)
        if record is None:
            raise SessionNotFound(f"session not found: {session_id}")
        if record.is_expired(float(self._clock())):
            raise SessionExpired(f"session expired: {session_id}")
        return copy.deepcopy(record)

    def delete(self, session_id: str) -> None:
        validate_session_id(session_id)
        self._records.pop(session_id, None)

    def list_sessions(self) -> tuple[str, ...]:
        return tuple(sorted(self._records))


class FileCheckpointStore:
    """一个会话一个 JSON 文件；写入先落临时文件再 `os.replace`（原子替换）。

    这条路径必须假设磁盘上的内容**可能被外部改写**：JSON 解析失败、schema 版本
    不符、文件名与内容不一致，一律 `SessionTampered`，绝不半信半疑地续跑。
    """

    def __init__(self, directory: str | os.PathLike[str], *,
                 clock: Callable[[], float] | None = None,
                 ttl_sec: float = DEFAULT_TTL_SEC,
                 max_request_log: int = MAX_REQUEST_LOG):
        if isinstance(max_request_log, bool) or not isinstance(max_request_log, int) or max_request_log <= 0:
            raise SessionError("max_request_log must be a positive integer")
        self._directory = os.path.abspath(os.fspath(directory))
        os.makedirs(self._directory, exist_ok=True)
        self._clock = clock or time.time
        self._ttl_sec = _positive_number(ttl_sec, "ttl_sec")
        self._max_request_log = max_request_log

    @property
    def directory(self) -> str:
        return self._directory

    @property
    def ttl_sec(self) -> float:
        return self._ttl_sec

    def path_for(self, session_id: str) -> str:
        """会话文件路径；先校验 id，再确认解析后的路径没有逃出存储目录。"""
        validated = validate_session_id(session_id)
        path = os.path.abspath(os.path.join(self._directory, f"{validated}.json"))
        if os.path.dirname(path) != self._directory:
            raise SessionError(f"session_id {session_id!r} escapes the store directory")
        return path

    def save(self, session_id: str, checkpoint: AgentCheckpoint, *,
             request: Mapping[str, Any] | None = None,
             response: Mapping[str, Any] | None = None,
             ttl_sec: float | None = None) -> SessionRecord:
        path = self.path_for(session_id)
        if not isinstance(checkpoint, AgentCheckpoint):
            raise SessionError("checkpoint must be an AgentCheckpoint")
        now = float(self._clock())
        ttl = self._ttl_sec if ttl_sec is None else _positive_number(ttl_sec, "ttl_sec")
        existing = self._read(path)
        if existing is None:
            created_at, revision, log = now, 1, ()
        else:
            created_at, revision, log = existing.created_at, existing.revision + 1, existing.request_log
        entry = _log_entry(request, response)
        if entry is not None:
            log = (log + (entry,))[-self._max_request_log:]
        record = SessionRecord(
            session_id=session_id,
            checkpoint=copy.deepcopy(checkpoint),
            created_at=created_at,
            updated_at=now,
            expires_at=now + ttl,
            revision=revision,
            request_log=log,
            schema_version=SESSION_SCHEMA_VERSION,
        )
        self._write_atomic(path, record.to_dict())
        return record

    def load(self, session_id: str) -> SessionRecord:
        path = self.path_for(session_id)
        record = self._read(path)
        if record is None:
            raise SessionNotFound(f"session not found: {session_id}")
        if record.is_expired(float(self._clock())):
            raise SessionExpired(f"session expired: {session_id}")
        return record

    def delete(self, session_id: str) -> None:
        path = self.path_for(session_id)
        try:
            os.unlink(path)
        except FileNotFoundError:
            return
        except OSError as exc:  # pragma: no cover - 依赖文件系统权限
            raise SessionError(f"session file cannot be deleted: {exc}") from exc

    def list_sessions(self) -> tuple[str, ...]:
        found: list[str] = []
        try:
            entries = list(os.scandir(self._directory))
        except OSError:  # pragma: no cover - 目录在运行中被删除
            return ()
        for entry in entries:
            if not entry.is_file() or not entry.name.endswith(".json"):
                continue
            stem = entry.name[: -len(".json")]
            if _SESSION_ID_PATTERN.fullmatch(stem):
                found.append(stem)
        return tuple(sorted(found))

    def _read(self, path: str) -> SessionRecord | None:
        """读并校验一个会话文件；不存在返回 None，坏文件抛 `SessionTampered`。"""
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except FileNotFoundError:
            return None
        except (UnicodeDecodeError, ValueError) as exc:
            raise SessionTampered(f"session file is not valid JSON: {path}") from exc
        except OSError as exc:
            raise SessionError(f"session file cannot be read: {exc}") from exc
        record = SessionRecord.from_dict(raw)
        expected = os.path.basename(path)[: -len(".json")]
        if record.session_id != expected:
            raise SessionTampered(
                f"session file {expected!r} contains session_id {record.session_id!r}")
        return record

    def _write_atomic(self, path: str, payload: Mapping[str, Any]) -> None:
        try:
            encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                 allow_nan=False, indent=2)
        except (TypeError, ValueError) as exc:
            raise SessionError(f"session record is not JSON-serializable: {exc}") from exc
        tmp_path: str | None = None
        try:
            handle_fd, tmp_path = tempfile.mkstemp(prefix=".session-", suffix=".tmp",
                                                   dir=self._directory)
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, path)
            tmp_path = None
        except OSError as exc:
            raise SessionError(f"session file cannot be written: {exc}") from exc
        finally:
            if tmp_path is not None:
                try:
                    os.unlink(tmp_path)
                except OSError:  # pragma: no cover - 清理失败不该掩盖真正的错误
                    pass


class SessionRunner:
    """把「加载检查点 -> 驱动循环 -> 保存结果」串成一次请求的处理单元。

    幂等：同一个 `request_id` 第二次到达时直接回放已存响应，**不再跑循环**。
    这就是「同一份确认提交两次不会产生两次副作用」的落点。

    跨进程续跑：`checkpoint` 从存储加载、执行后立刻写回，因此调用方（HTTP 层）
    不需要持有上一进程里的任何 Python 对象。授权判定仍由 `BoundedAgentLoop`
    对 user_input + context + runtime_context + tool call 的绑定完成。
    """

    def __init__(self, loop: Any, store: CheckpointStore, *, max_rounds: int = 12):
        if isinstance(max_rounds, bool) or not isinstance(max_rounds, int) or max_rounds <= 0:
            raise ValueError("max_rounds must be a positive integer")
        if not all(callable(getattr(store, name, None)) for name in ("save", "load")):
            raise ValueError("store must provide save() and load()")
        self._loop = loop
        self._store = store
        self._max_rounds = max_rounds

    @property
    def store(self) -> CheckpointStore:
        return self._store

    @property
    def max_rounds(self) -> int:
        return self._max_rounds

    def handle(self, session_id: str, *, text: str, confirmed: bool = False,
               request_id: str | None = None,
               context: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """处理一条会话消息，返回稳定键集合（见 `RESPONSE_KEYS`）的响应。"""
        record = self._load_record(session_id)

        if record is not None and request_id is not None:
            stored = record.response_for(request_id)
            if stored is not None:
                return stored

        checkpoint: AgentCheckpoint | None = None
        if record is not None:
            if record.checkpoint.user_input != text:
                # 换了任务：旧检查点与旧请求日志一起丢弃，否则旧 request_id 会把
                # 新任务的请求答成上一个任务的响应。
                self._store.delete(session_id)
                record = None
            else:
                checkpoint = record.checkpoint

        # 「本次调用是否被授权连续推进」：调用方显式 confirmed=True，或者上一请求
        # 已经把检查点停在确认握手上（等价于 run_cockpit 的 auto-confirm）。
        # 注意这只是**客户端声明的意图**；真正的授权仍是循环里的确认绑定校验。
        resume_authorized = bool(confirmed) or (
            checkpoint is not None and checkpoint.status is LoopStatus.CLARIFICATION)

        result = self._loop.run(text, context=context, checkpoint=checkpoint,
                                confirmed=bool(confirmed) or checkpoint is not None)
        rounds = 1
        while (result.status is LoopStatus.CLARIFICATION and resume_authorized
               and rounds < self._max_rounds):
            result = self._loop.run(text, context=context, checkpoint=result.checkpoint,
                                    confirmed=True)
            rounds += 1

        revision = 1 if record is None else record.revision + 1
        response = self._response(session_id, result, request_id, revision)
        saved = self._store.save(
            session_id, result.checkpoint,
            request={"request_id": request_id} if request_id else None,
            response=response)
        if saved.revision != response["checkpoint_revision"]:
            # 存储实现违反了「每次 save 自增 1」的契约：宁可如实报告存储里的版本号，
            # 也不能返回一个与存储不一致的数字。
            response["checkpoint_revision"] = saved.revision
        return response

    def _load_record(self, session_id: str) -> SessionRecord | None:
        try:
            return self._store.load(session_id)
        except SessionNotFound:
            return None
        except SessionExpired:
            # 过期的会话等同不存在。显式删除，免得新的会话继承旧的 request_log，
            # 让一个早已过期的幂等键把新请求答成旧响应。
            self._store.delete(session_id)
            return None

    @staticmethod
    def _response(session_id: str, result: Any, request_id: str | None,
                  revision: int) -> dict[str, Any]:
        checkpoint = result.checkpoint
        tool_call = None
        if result.tool_call is not None:
            tool_call = {
                "name": result.tool_call.name,
                "arguments": dict(result.tool_call.arguments),
            }
        return {
            "session_id": session_id,
            "status": result.status.value,
            "message": result.message,
            "step_count": checkpoint.step_count,
            "max_steps": checkpoint.max_steps,
            "executed": [entry.get("name") for entry in (checkpoint.executed_tool_calls or [])],
            "tool_call": tool_call,
            "error": dict(checkpoint.last_error) if checkpoint.last_error else None,
            "checkpoint_revision": revision,
            "request_id": request_id,
        }
