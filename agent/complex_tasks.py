"""Persistent queue executor for Canvas Agent ``/批量任务``.

The module deliberately knows nothing about FastAPI or upstream provider
implementations. ``agent.backend`` supplies the narrow callbacks used to
create canvas nodes and submit/poll provider jobs.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
import sqlite3
import time
import urllib.parse
import uuid
from pathlib import Path
from threading import Lock
from typing import Any, Awaitable, Callable, Dict, Iterable, List, Optional, Set


TERMINAL_TASK_STATES = {"completed", "partially_completed", "failed", "cancelled"}
TERMINAL_ITEM_STATES = {"completed", "failed", "cancelled", "blocked", "interrupted"}
RUNNABLE_ITEM_STATES = {"queued", "retrying"}
ALLOWED_MODES = {"deterministic", "agentic"}
ALLOWED_STAGE_TYPES = {"canvas_operation", "prompt", "generate_image", "generate_video", "review"}
ALLOWED_LINK_VISIBILITY = {"visible", "main", "hidden"}
REVIEW_DECISIONS = {"accept", "retry", "revise_prompt", "block", "ask_user"}


class ComplexTaskError(ValueError):
    pass


def _now_ms() -> int:
    return int(time.time() * 1000)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value or ""))
        return parsed
    except Exception:
        return fallback


def _reference_display_url(value: Any) -> str:
    """Return a browser-safe preview URL without changing the provider source URL."""
    text = str(value or "").strip()
    if not text:
        return ""
    lowered = text.lower()
    if lowered.startswith(("data:", "blob:", "http://", "https://")):
        return text
    if text.startswith(("/assets/", "/output/", "/api/")):
        return text
    if lowered.startswith("file://"):
        text = urllib.parse.unquote(text[len("file://"):])
    return "/api/codex-agent/file/view?path=" + urllib.parse.quote(text, safe="")


def _planned_media_node_size(payload: Dict[str, Any], kind: str) -> tuple[int, int]:
    """Mirror the smart-canvas single-media fit box before the media has loaded."""
    candidates = [
        payload.get("size"),
        payload.get("aspect_ratio"),
        payload.get("aspect"),
        payload.get("ratio"),
    ]
    ratio_aliases = {
        "square": (1.0, 1.0),
        "portrait": (3.0, 4.0),
        "vertical": (9.0, 16.0),
        "landscape": (4.0, 3.0),
        "horizontal": (16.0, 9.0),
    }
    natural_w = natural_h = 0.0
    for raw in candidates:
        text = str(raw or "").strip().lower()
        if not text:
            continue
        if text in ratio_aliases:
            natural_w, natural_h = ratio_aliases[text]
            break
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[x×*:：/]\s*(\d+(?:\.\d+)?)\s*", text)
        if match:
            natural_w, natural_h = float(match.group(1)), float(match.group(2))
            if natural_w > 0 and natural_h > 0:
                break
    if natural_w <= 0 or natural_h <= 0:
        natural_w, natural_h = ((16.0, 9.0) if kind == "generate_video" else (1.0, 1.0))
    # Keep this aligned with singleImageLayout() at MEDIA_NODE_DEFAULT_SCALE=2.
    fit = min(520.0 / natural_w, 440.0 / natural_h)
    return max(72, round(natural_w * fit)), max(72, round(natural_h * fit))


def infer_complex_task_mode(spec: Dict[str, Any]) -> str:
    requested = str(spec.get("mode") or "auto").strip().lower()
    if requested in ALLOWED_MODES:
        return requested
    for stage in spec.get("stages") or []:
        if not isinstance(stage, dict):
            continue
        acceptance = stage.get("acceptance") if isinstance(stage.get("acceptance"), dict) else {}
        if str(stage.get("type") or "") == "review" or str(acceptance.get("review") or "").lower() == "agent":
            return "agentic"
        if stage.get("conditional") or stage.get("agent_checkpoint"):
            return "agentic"
    return "deterministic"


def normalize_complex_task_spec(raw: Dict[str, Any], *, canvas_id: str = "") -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise ComplexTaskError("任务规格必须是对象")
    stages_raw = raw.get("stages")
    if not isinstance(stages_raw, list) or not stages_raw:
        raise ComplexTaskError("任务至少需要一个 stage")
    limits = dict(raw.get("limits") or {}) if isinstance(raw.get("limits"), dict) else {}
    max_items = max(1, min(2000, int(limits.get("max_items") or 200)))
    max_attempts = max(1, min(2000, int(limits.get("max_total_attempts") or 300)))
    max_checkpoints = max(1, min(100, int(limits.get("max_agent_checkpoints") or 20)))
    max_retries = max(0, min(10, int(limits.get("max_retries_per_item") if limits.get("max_retries_per_item") is not None else 2)))
    stages: List[Dict[str, Any]] = []
    all_item_ids: Set[str] = set()
    stage_ids: Set[str] = set()
    item_count = 0
    for stage_index, source in enumerate(stages_raw):
        if not isinstance(source, dict):
            raise ComplexTaskError(f"第 {stage_index + 1} 个 stage 格式不正确")
        stage_id = str(source.get("id") or f"stage_{stage_index + 1}").strip()
        if not stage_id or stage_id in stage_ids:
            raise ComplexTaskError(f"stage id 重复或为空：{stage_id}")
        stage_ids.add(stage_id)
        stage_type = str(source.get("type") or "canvas_operation").strip().lower()
        if stage_type not in ALLOWED_STAGE_TYPES:
            raise ComplexTaskError(f"不支持的 stage 类型：{stage_type}")
        stage_depends = [str(value) for value in source.get("depends_on") or [] if str(value)]
        items_raw = source.get("items")
        if isinstance(items_raw, int):
            items_raw = [{"key": str(index + 1)} for index in range(max(0, items_raw))]
        if items_raw is None and stage_type == "review":
            items_raw = [{"key": "review"}]
        if not isinstance(items_raw, list) or not items_raw:
            raise ComplexTaskError(f"stage {stage_id} 至少需要一个 item")
        normalized_items = []
        for item_index, item_source in enumerate(items_raw):
            item = {"payload": item_source} if isinstance(item_source, str) else dict(item_source or {})
            item_id = str(item.get("id") or f"{stage_id}_{item_index + 1}").strip()
            if not item_id or item_id in all_item_ids:
                raise ComplexTaskError(f"item id 重复或为空：{item_id}")
            all_item_ids.add(item_id)
            key = str(item.get("key") or item.get("shot_id") or item.get("index") or item_index + 1)
            payload = item.get("payload") if isinstance(item.get("payload"), dict) else {
                k: v for k, v in item.items() if k not in {"id", "key", "depends_on", "source_item_id", "title"}
            }
            normalized_items.append({
                "id": item_id,
                "key": key,
                "title": str(item.get("title") or payload.get("title") or f"{stage_id} {item_index + 1}"),
                "depends_on": [str(value) for value in item.get("depends_on") or [] if str(value)],
                "source_item_id": str(item.get("source_item_id") or ""),
                "payload": payload,
            })
        item_count += len(normalized_items)
        stages.append({
            "id": stage_id,
            "title": str(source.get("title") or stage_id),
            "type": stage_type,
            "depends_on": stage_depends,
            "unlock": "stream" if str(source.get("unlock") or source.get("dependency_mode") or "barrier").lower() in {"stream", "pipeline", "item"} else "barrier",
            "acceptance": dict(source.get("acceptance") or {}) if isinstance(source.get("acceptance"), dict) else {},
            "agent_checkpoint": bool(source.get("agent_checkpoint")),
            "items": normalized_items,
        })
    if item_count > max_items:
        raise ComplexTaskError(f"任务包含 {item_count} 个 item，超过当前上限 {max_items}")
    for stage in stages:
        missing_stages = [value for value in stage["depends_on"] if value not in stage_ids]
        if missing_stages:
            raise ComplexTaskError(f"stage {stage['id']} 引用了不存在的 stage：{', '.join(missing_stages)}")
        for item in stage["items"]:
            missing_items = [value for value in item["depends_on"] if value not in all_item_ids]
            if missing_items:
                raise ComplexTaskError(f"item {item['id']} 引用了不存在的 item：{', '.join(missing_items)}")
    _assert_acyclic(stages)
    cost_boundary = dict(raw.get("cost_boundary") or {}) if isinstance(raw.get("cost_boundary"), dict) else {}
    provider_calls = sum(len(stage["items"]) for stage in stages if stage["type"] in {"generate_image", "generate_video"})
    if cost_boundary.get("max_provider_calls") is not None:
        max_provider_calls = max(0, int(cost_boundary.get("max_provider_calls") or 0))
        if provider_calls > max_provider_calls:
            raise ComplexTaskError(f"预计 Provider 调用 {provider_calls} 次，超过费用边界 {max_provider_calls} 次")
    return {
        "schema_version": 1,
        "title": str(raw.get("title") or "批量任务")[:160],
        "mode": infer_complex_task_mode({**raw, "stages": stages}),
        "canvas_id": str(raw.get("canvas_id") or canvas_id),
        "project_dir": str(raw.get("project_dir") or ""),
        "conversation_id": str(raw.get("conversation_id") or ""),
        "base_revision": max(0, int(raw.get("base_revision") or 0)),
        "link_visibility": str(raw.get("link_visibility") or "visible") if str(raw.get("link_visibility") or "visible") in ALLOWED_LINK_VISIBILITY else "visible",
        "concurrency": dict(raw.get("concurrency") or {}) if isinstance(raw.get("concurrency"), dict) else {},
        "cost_boundary": cost_boundary,
        "completion": dict(raw.get("completion") or {}) if isinstance(raw.get("completion"), dict) else {},
        "limits": {
            "max_items": max_items,
            "max_total_attempts": max_attempts,
            "max_agent_checkpoints": max_checkpoints,
            "max_retries_per_item": max_retries,
        },
        "options": dict(raw.get("options") or {}) if isinstance(raw.get("options"), dict) else {},
        "stages": stages,
    }


def normalize_batch_task_spec(raw: Dict[str, Any], *, canvas_id: str = "") -> Dict[str, Any]:
    """Convert a strict flat batch into the existing persistent execution model.

    The public Batch Tool never accepts free-form stages or canvas actions.  Each
    item is an independent provider job; the internal stage rows are only a
    storage/execution detail retained for compatibility with existing history.
    """
    if not isinstance(raw, dict):
        raise ComplexTaskError("批量任务规格必须是对象")
    items_raw = raw.get("items")
    if not isinstance(items_raw, list) or not items_raw:
        raise ComplexTaskError("批量任务至少需要一个节点项目")
    if len(items_raw) > 200:
        raise ComplexTaskError("批量任务最多支持 200 个节点项目")

    image_items: List[Dict[str, Any]] = []
    video_items: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for index, source in enumerate(items_raw):
        if not isinstance(source, dict):
            raise ComplexTaskError(f"第 {index + 1} 个批量项目格式不正确")
        kind = str(source.get("kind") or source.get("type") or "image").strip().lower()
        if kind in {"generate_image", "image_generation"}:
            kind = "image"
        elif kind in {"generate_video", "video_generation"}:
            kind = "video"
        if kind not in {"image", "video"}:
            raise ComplexTaskError(f"第 {index + 1} 个批量项目类型不支持：{kind}")
        prompt = str(source.get("prompt") or source.get("text") or "").strip()
        if not prompt:
            raise ComplexTaskError(f"第 {index + 1} 个批量项目缺少提示词")
        provider_id = str(source.get("provider_id") or "").strip()
        if not provider_id:
            raise ComplexTaskError(f"第 {index + 1} 个批量项目缺少生成平台")
        item_id = str(source.get("id") or f"batch_{index + 1}").strip()
        if not item_id or item_id in seen:
            raise ComplexTaskError(f"批量项目 id 重复或为空：{item_id}")
        seen.add(item_id)
        payload = {
            key: value for key, value in source.items()
            if key not in {"id", "key", "title", "kind", "type"}
        }
        payload["prompt"] = prompt
        payload["provider_id"] = provider_id
        target = image_items if kind == "image" else video_items
        target.append({
            "id": item_id,
            "key": str(source.get("key") or index + 1),
            "title": str(source.get("title") or f"{provider_id} {'生图' if kind == 'image' else '视频'} {index + 1}"),
            "payload": payload,
        })

    concurrency_raw = raw.get("concurrency") if isinstance(raw.get("concurrency"), dict) else {}
    provider_limits = concurrency_raw.get("providers") if isinstance(concurrency_raw.get("providers"), dict) else {}
    concurrency: Dict[str, int] = {
        "global": max(1, min(20, int(concurrency_raw.get("global") or raw.get("global_concurrency") or 4)))
    }
    for provider_id, limit in provider_limits.items():
        concurrency[str(provider_id)] = max(1, min(20, int(limit)))
    retries = max(0, min(5, int(raw.get("max_retries") if raw.get("max_retries") is not None else 2)))
    stages: List[Dict[str, Any]] = []
    if image_items:
        stages.append({"id": "batch_images", "title": "批量生图", "type": "generate_image", "items": image_items})
    if video_items:
        stages.append({"id": "batch_videos", "title": "批量视频", "type": "generate_video", "items": video_items})
    return normalize_complex_task_spec({
        "title": str(raw.get("title") or "批量任务"),
        "mode": "deterministic",
        "canvas_id": str(raw.get("canvas_id") or canvas_id),
        "base_revision": max(0, int(raw.get("base_revision") or 0)),
        "link_visibility": str(raw.get("link_visibility") or "visible"),
        "concurrency": concurrency,
        "limits": {
            "max_items": len(items_raw),
            "max_total_attempts": max(len(items_raw), len(items_raw) * (retries + 1)),
            "max_agent_checkpoints": 1,
            "max_retries_per_item": retries,
        },
        "options": dict(raw.get("options") or {}) if isinstance(raw.get("options"), dict) else {},
        "stages": stages,
    }, canvas_id=canvas_id)


def _assert_acyclic(stages: List[Dict[str, Any]]) -> None:
    graph: Dict[str, Set[str]] = {}
    for stage in stages:
        graph[f"stage:{stage['id']}"] = {f"stage:{value}" for value in stage.get("depends_on") or []}
        for item in stage.get("items") or []:
            graph[f"item:{item['id']}"] = {f"item:{value}" for value in item.get("depends_on") or []}
    visiting: Set[str] = set()
    visited: Set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ComplexTaskError("任务依赖存在循环")
        if node in visited:
            return
        visiting.add(node)
        for dep in graph.get(node, set()):
            visit(dep)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)


class ComplexTaskEngine:
    def __init__(
        self,
        db_path: Path,
        config_path: Path,
        *,
        load_canvas: Callable[[str], Dict[str, Any]],
        save_canvas: Callable[[Dict[str, Any]], Any],
        broadcast_canvas: Callable[[str, int], Awaitable[None]],
        submit_generation: Callable[[str, str, Dict[str, Any], str], Awaitable[Dict[str, Any]]],
        poll_generation: Callable[[str], Awaitable[Dict[str, Any]]],
        run_checkpoint: Optional[Callable[[Dict[str, Any], Dict[str, Any]], Awaitable[Dict[str, Any]]]] = None,
        revision_observer: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
        initialize: bool = True,
    ):
        self.db_path = Path(db_path)
        self.config_path = Path(config_path)
        self.load_canvas = load_canvas
        self.save_canvas = save_canvas
        self.broadcast_canvas = broadcast_canvas
        self.submit_generation = submit_generation
        self.poll_generation = poll_generation
        self.run_checkpoint = run_checkpoint
        self.revision_observer = revision_observer
        self._lock = Lock()
        self._workers: Dict[str, asyncio.Task] = {}
        self._global_sem = asyncio.Semaphore(4)
        self._provider_sems: Dict[str, asyncio.Semaphore] = {}
        self._provider_failures: Dict[str, int] = {}
        self._provider_circuit_until: Dict[str, float] = {}
        if initialize:
            self._init_db()

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS complex_tasks (
                    id TEXT PRIMARY KEY, canvas_id TEXT NOT NULL, project_dir TEXT NOT NULL DEFAULT '',
                    conversation_id TEXT NOT NULL DEFAULT '', task_thread_id TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL, mode TEXT NOT NULL, status TEXT NOT NULL,
                    base_revision INTEGER NOT NULL DEFAULT 0, link_visibility TEXT NOT NULL DEFAULT 'visible',
                    spec_json TEXT NOT NULL, summary_json TEXT NOT NULL DEFAULT '{}',
                    question TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '',
                    checkpoint_count INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS complex_task_stages (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, stage_order INTEGER NOT NULL,
                    type TEXT NOT NULL, title TEXT NOT NULL, status TEXT NOT NULL,
                    depends_json TEXT NOT NULL DEFAULT '[]', spec_json TEXT NOT NULL,
                    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS complex_task_items (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, stage_id TEXT NOT NULL, item_order INTEGER NOT NULL,
                    item_key TEXT NOT NULL DEFAULT '', title TEXT NOT NULL, status TEXT NOT NULL,
                    depends_json TEXT NOT NULL DEFAULT '[]', payload_json TEXT NOT NULL,
                    node_id TEXT NOT NULL DEFAULT '', provider_task_id TEXT NOT NULL DEFAULT '',
                    attempt_count INTEGER NOT NULL DEFAULT 0, retry_after INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '', result_json TEXT NOT NULL DEFAULT '{}',
                    idempotency_key TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS complex_task_attempts (
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, item_id TEXT NOT NULL, attempt_no INTEGER NOT NULL,
                    status TEXT NOT NULL, provider_task_id TEXT NOT NULL DEFAULT '', payload_json TEXT NOT NULL,
                    result_json TEXT NOT NULL DEFAULT '{}', error TEXT NOT NULL DEFAULT '', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS complex_task_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_complex_tasks_canvas ON complex_tasks(canvas_id, updated_at);
                CREATE INDEX IF NOT EXISTS idx_complex_items_task_status ON complex_task_items(task_id, status, item_order);
                CREATE INDEX IF NOT EXISTS idx_complex_events_task ON complex_task_events(task_id, id);
                """)
                item_columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(complex_task_items)").fetchall()}
                if "retry_after" not in item_columns:
                    conn.execute("ALTER TABLE complex_task_items ADD COLUMN retry_after INTEGER NOT NULL DEFAULT 0")
                conn.commit()
            finally:
                conn.close()

    def config(self) -> Dict[str, Any]:
        defaults = {"global": 4, "gpt_image": 3, "unknown_image": 1, "video": 1, "providers": {}}
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                defaults.update(data)
        except Exception:
            pass
        return defaults

    async def startup(self) -> None:
        self._init_db()
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute("SELECT id,status FROM complex_tasks WHERE status NOT IN ('completed','partially_completed','failed','cancelled')").fetchall()
                conn.execute("UPDATE complex_task_items SET status='interrupted', error='服务重启时 Provider 尚未返回可恢复任务号' WHERE status='submitting' AND provider_task_id=''")
                conn.execute("UPDATE complex_task_items SET status='waiting_provider' WHERE status IN ('submitting','running') AND provider_task_id!=''")
                conn.execute("UPDATE complex_task_items SET status='queued' WHERE status='running' AND provider_task_id='' ")
                conn.execute("UPDATE complex_tasks SET status='queued', updated_at=? WHERE status IN ('running','reviewing','waiting_provider','retrying')", (_now_ms(),))
                conn.commit()
            finally:
                conn.close()
        for row in rows:
            if str(row["status"]) not in {"paused", "waiting_user"}:
                self.schedule(str(row["id"]))

    def create(self, raw_spec: Dict[str, Any], *, canvas_id: str = "") -> Dict[str, Any]:
        spec = normalize_complex_task_spec(raw_spec, canvas_id=canvas_id)
        if not spec["canvas_id"]:
            raise ComplexTaskError("缺少 canvas_id")
        if self.revision_observer:
            observed = self.revision_observer(spec["canvas_id"], self.load_canvas(spec["canvas_id"]))
            if not spec["base_revision"]:
                spec["base_revision"] = max(0, int(observed.get("revision") or 0))
            spec.setdefault("runtime", {})["last_observed_revision"] = max(0, int(observed.get("revision") or 0))
        task_id = uuid.uuid4().hex
        now = _now_ms()
        task_node_id = f"agenttask_{uuid.uuid4().hex[:16]}"
        total_items = sum(len(stage["items"]) for stage in spec["stages"])
        summary = {"task_node_id": task_node_id, "total": total_items, "processed": 0, "completed": 0, "failed": 0, "running": 0, "retrying": 0, "waiting": total_items, "current_stage": spec["stages"][0]["title"]}
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("INSERT INTO complex_tasks(id,canvas_id,project_dir,conversation_id,title,mode,status,base_revision,link_visibility,spec_json,summary_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                    task_id, spec["canvas_id"], spec["project_dir"], spec["conversation_id"], spec["title"], spec["mode"], "queued", spec["base_revision"], spec["link_visibility"], _json(spec), _json(summary), now, now,
                ))
                stage_item_ids = {stage["id"]: [item["id"] for item in stage["items"]] for stage in spec["stages"]}
                stage_items_by_key = {stage["id"]: {item["key"]: item["id"] for item in stage["items"]} for stage in spec["stages"]}
                for stage_order, stage in enumerate(spec["stages"]):
                    conn.execute("INSERT INTO complex_task_stages(id,task_id,stage_order,type,title,status,depends_json,spec_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (
                        f"{task_id}:{stage['id']}", task_id, stage_order, stage["type"], stage["title"], "queued", _json(stage["depends_on"]), _json(stage), now, now,
                    ))
                    for item_order, item in enumerate(stage["items"]):
                        dependencies = list(item["depends_on"])
                        if item["source_item_id"]:
                            dependencies.append(item["source_item_id"])
                        if not dependencies and stage["depends_on"]:
                            for dep_stage in stage["depends_on"]:
                                if stage["unlock"] == "stream":
                                    match = stage_items_by_key.get(dep_stage, {}).get(item["key"])
                                    if match:
                                        dependencies.append(match)
                                else:
                                    dependencies.extend(stage_item_ids.get(dep_stage, []))
                        conn.execute("INSERT INTO complex_task_items(id,task_id,stage_id,item_order,item_key,title,status,depends_json,payload_json,idempotency_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                            f"{task_id}:{item['id']}", task_id, stage["id"], item_order, item["key"], item["title"], "queued", _json(list(dict.fromkeys(dependencies))), _json(item["payload"]), f"{task_id}:{item['id']}:1", now, now,
                        ))
                conn.execute("INSERT INTO complex_task_events(task_id,event_type,payload_json,created_at) VALUES(?,?,?,?)", (task_id, "task_created", _json({"mode": spec["mode"], "total": summary["total"]}), now))
                conn.commit()
            finally:
                conn.close()
        self._create_task_node(task_id, summary)
        self.schedule(task_id)
        return self.get(task_id)

    def schedule(self, task_id: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        current = self._workers.get(task_id)
        if current and not current.done():
            if current is not asyncio.current_task():
                current.add_done_callback(lambda _future, value=task_id: self.schedule(value))
            return
        self._workers[task_id] = loop.create_task(self._run(task_id))

    def get(self, task_id: str, *, after: int = 0, include_items: bool = True) -> Dict[str, Any]:
        with self._lock:
            conn = self._connect()
            try:
                task = conn.execute("SELECT * FROM complex_tasks WHERE id=?", (task_id,)).fetchone()
                if not task:
                    raise ComplexTaskError("批量任务不存在")
                stages = conn.execute("SELECT * FROM complex_task_stages WHERE task_id=? ORDER BY stage_order", (task_id,)).fetchall()
                items = conn.execute("SELECT * FROM complex_task_items WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall() if include_items else []
                events = conn.execute("SELECT * FROM complex_task_events WHERE task_id=? AND id>? ORDER BY id LIMIT 300", (task_id, max(0, int(after)))).fetchall()
            finally:
                conn.close()
        data = dict(task)
        data["spec"] = _load_json(data.pop("spec_json", "{}"), {})
        data["summary"] = _load_json(data.pop("summary_json", "{}"), {})
        data["stages"] = [{**dict(row), "depends_on": _load_json(row["depends_json"], []), "spec": _load_json(row["spec_json"], {})} for row in stages]
        data["items"] = [{**dict(row), "depends_on": _load_json(row["depends_json"], []), "payload": _load_json(row["payload_json"], {}), "result": _load_json(row["result_json"], {})} for row in items]
        data["events"] = [{"id": row["id"], "type": row["event_type"], "payload": _load_json(row["payload_json"], {}), "created_at": row["created_at"]} for row in events]
        data["next_event_id"] = events[-1]["id"] if events else max(0, int(after))
        return data

    def list(self, canvas_id: str = "", *, active_only: bool = False) -> List[Dict[str, Any]]:
        clauses, values = [], []
        if canvas_id:
            clauses.append("canvas_id=?")
            values.append(canvas_id)
        if active_only:
            clauses.append("status NOT IN ('completed','partially_completed','failed','cancelled')")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(f"SELECT id FROM complex_tasks{where} ORDER BY updated_at DESC LIMIT 100", values).fetchall()
            finally:
                conn.close()
        return [self.get(str(row["id"]), include_items=False) for row in rows]

    def control(self, task_id: str, action: str) -> Dict[str, Any]:
        action = str(action or "").strip().lower()
        task = self.get(task_id, include_items=False)
        if action == "pause":
            self._set_task(task_id, "paused")
        elif action == "resume":
            if task["status"] not in TERMINAL_TASK_STATES:
                self._set_task(task_id, "queued", question="", error="")
                self.schedule(task_id)
        elif action == "cancel":
            self._cancel(task_id)
        elif action in {"retry", "retry_failed"}:
            with self._lock:
                conn = self._connect()
                try:
                    conn.execute("UPDATE complex_task_items SET status='retrying', retry_after=0, error='', updated_at=? WHERE task_id=? AND status IN ('failed','interrupted','blocked')", (_now_ms(), task_id))
                    conn.commit()
                finally:
                    conn.close()
            self._set_task(task_id, "queued", question="", error="")
            self.schedule(task_id)
        elif action in {"links_visible", "links_main", "links_hidden"}:
            visibility = action.replace("links_", "")
            self._set_task(task_id, str(task.get("status") or "queued"), link_visibility=visibility)
        else:
            raise ComplexTaskError("不支持的任务控制动作")
        self._event(task_id, f"task_{action}", {})
        self._sync_task_node(task_id)
        return self.get(task_id)

    def reply(self, task_id: str, text: str) -> Dict[str, Any]:
        task = self.get(task_id, include_items=False)
        if task["status"] != "waiting_user":
            raise ComplexTaskError("当前任务没有等待用户回答")
        self._event(task_id, "user_reply", {"text": str(text or "")[:12000]})
        with self._lock:
            conn = self._connect()
            try:
                spec = task["spec"]
                spec.setdefault("runtime", {})["last_user_reply"] = str(text or "")[:12000]
                conn.execute("UPDATE complex_tasks SET status='queued', question='', spec_json=?, updated_at=? WHERE id=?", (_json(spec), _now_ms(), task_id))
                conn.commit()
            finally:
                conn.close()
        self.schedule(task_id)
        return self.get(task_id)

    def revise(self, task_id: str, changes: List[Dict[str, Any]]) -> Dict[str, Any]:
        if not isinstance(changes, list):
            raise ComplexTaskError("changes 必须是数组")
        changed = 0
        with self._lock:
            conn = self._connect()
            try:
                for change in changes[:200]:
                    if not isinstance(change, dict):
                        continue
                    item_id = str(change.get("item_id") or "")
                    full_id = item_id if item_id.startswith(task_id + ":") else f"{task_id}:{item_id}"
                    row = conn.execute("SELECT status,payload_json FROM complex_task_items WHERE id=? AND task_id=?", (full_id, task_id)).fetchone()
                    if not row or row["status"] not in {"queued", "retrying", "blocked", "interrupted"}:
                        continue
                    payload = _load_json(row["payload_json"], {})
                    patch = change.get("payload") if isinstance(change.get("payload"), dict) else {}
                    payload.update(patch)
                    conn.execute("UPDATE complex_task_items SET payload_json=?, status='queued', retry_after=0, error='', updated_at=? WHERE id=?", (_json(payload), _now_ms(), full_id))
                    changed += 1
                conn.commit()
            finally:
                conn.close()
        self._event(task_id, "task_revised", {"changed": changed})
        if changed:
            self._set_task(task_id, "queued", question="", error="")
            self.schedule(task_id)
        return self.get(task_id)

    def submit_review(self, task_id: str, review: Dict[str, Any]) -> Dict[str, Any]:
        decision = str(review.get("decision") or "").strip().lower()
        if decision not in REVIEW_DECISIONS:
            raise ComplexTaskError("无效的验收决定")
        self._event(task_id, "agent_review", review)
        if decision == "ask_user":
            self._set_task(task_id, "waiting_user", question=str(review.get("question") or "需要用户确认后继续"))
        elif decision == "block":
            self._set_task(task_id, "waiting_user", question=str(review.get("reason") or "任务需要用户处理"))
        elif decision in {"retry", "revise_prompt"}:
            item_ids = [str(value) for value in review.get("item_ids") or []]
            changes = review.get("changes") if isinstance(review.get("changes"), list) else []
            targeted = {value if value.startswith(task_id + ":") else f"{task_id}:{value}" for value in item_ids}
            deferred_changes = []
            task_canvas = self.load_canvas(self.get(task_id, include_items=False)["canvas_id"])
            live_nodes = {str(node.get("id") or ""): node for node in task_canvas.get("nodes") or [] if isinstance(node, dict)}
            with self._lock:
                conn = self._connect()
                try:
                    for change in changes:
                        if not isinstance(change, dict):
                            continue
                        value = str(change.get("item_id") or "")
                        full_id = value if value.startswith(task_id + ":") else f"{task_id}:{value}"
                        if full_id not in targeted:
                            deferred_changes.append(change)
                            continue
                        row = conn.execute("SELECT payload_json FROM complex_task_items WHERE id=? AND task_id=?", (full_id, task_id)).fetchone()
                        if row:
                            payload = _load_json(row["payload_json"], {})
                            patch = change.get("payload") if isinstance(change.get("payload"), dict) else {}
                            payload.update(patch)
                            conn.execute("UPDATE complex_task_items SET payload_json=?, updated_at=? WHERE id=?", (_json(payload), _now_ms(), full_id))
                    for value in item_ids:
                        full_id = value if value.startswith(task_id + ":") else f"{task_id}:{value}"
                        row = conn.execute("SELECT node_id FROM complex_task_items WHERE id=? AND task_id=?", (full_id, task_id)).fetchone()
                        node_id = str(row["node_id"] or "") if row else ""
                        live = live_nodes.get(node_id) if node_id else None
                        short_id = self._short_id(task_id, full_id)
                        keep_node = bool(live and str(live.get("complexTaskId") or "") == task_id and str(live.get("complexTaskItemId") or "") == short_id)
                        conn.execute("UPDATE complex_task_items SET status='retrying', retry_after=0, node_id=?, error='', updated_at=? WHERE id=? AND task_id=?", (node_id if keep_node else "", _now_ms(), full_id, task_id))
                    conn.commit()
                finally:
                    conn.close()
            if deferred_changes:
                self.revise(task_id, deferred_changes)
            self._set_task(task_id, "queued")
            self.schedule(task_id)
        else:
            self._set_task(task_id, "queued", question="")
            self.schedule(task_id)
        return self.get(task_id)

    async def _run(self, task_id: str) -> None:
        try:
            while True:
                task = self.get(task_id)
                if task["status"] in TERMINAL_TASK_STATES | {"paused", "waiting_user"}:
                    return
                self._set_task(task_id, "running")
                self._sync_task_node(task_id)
                if self.revision_observer:
                    self.revision_observer(task["canvas_id"], self.load_canvas(task["canvas_id"]))
                checkpoint_stage = self._next_checkpoint_stage(task)
                if task["mode"] == "agentic" and checkpoint_stage:
                    decision = await self._checkpoint(task_id, checkpoint_stage)
                    if decision == "accept":
                        self._mark_checkpoint_done(task_id, checkpoint_stage)
                        continue
                    if decision in {"retry", "revise_prompt"}:
                        continue
                    return
                runnable = self._runnable_items(task)
                if runnable:
                    await asyncio.gather(*(self._execute_item(task_id, item) for item in runnable))
                    self._refresh(task_id)
                    continue
                task = self.get(task_id)
                active = [item for item in task["items"] if item["status"] in {"submitting", "running", "waiting_provider"}]
                pending = [item for item in task["items"] if item["status"] in {"queued", "retrying"}]
                failed = [item for item in task["items"] if item["status"] in {"failed", "blocked", "interrupted"}]
                if active:
                    recoverable = [item for item in active if item["status"] == "waiting_provider" and item.get("provider_task_id")]
                    if recoverable:
                        await asyncio.gather(*(self._recover_provider_item(task_id, item) for item in recoverable))
                        self._refresh(task_id)
                        await asyncio.sleep(0.5)
                        continue
                    await asyncio.sleep(0.5)
                    continue
                if pending:
                    delayed = [item for item in pending if item["status"] == "retrying" and int(item.get("retry_after") or 0) > _now_ms()]
                    if delayed and len(delayed) == len(pending):
                        nearest = min(int(item.get("retry_after") or 0) for item in delayed)
                        await asyncio.sleep(max(0.05, min(1.0, (nearest - _now_ms()) / 1000)))
                        continue
                    self._set_task(task_id, "waiting_user", question="任务依赖未满足或相关节点已变化，请检查后继续")
                    self._sync_task_node(task_id)
                    return
                if task["mode"] == "agentic" and self._needs_final_checkpoint(task):
                    decision = await self._checkpoint(task_id, "__final__")
                    if decision == "accept":
                        self._mark_checkpoint_done(task_id, "__final__")
                        continue
                    if decision in {"retry", "revise_prompt"}:
                        continue
                    return
                final = "partially_completed" if failed and any(item["status"] == "completed" for item in task["items"]) else "failed" if failed else "completed"
                try:
                    arranged = self._arrange_task_nodes(task_id)
                    if arranged:
                        self._event(task_id, "task_nodes_arranged", {"count": arranged})
                except Exception as exc:
                    # Layout is a non-destructive finishing step and must not turn a
                    # completed paid generation task into a failed task.
                    self._event(task_id, "task_layout_failed", {"error": str(exc)[:1000]})
                self._set_task(task_id, final)
                self._event(task_id, "task_finished", {"status": final})
                self._refresh(task_id)
                return
        except asyncio.CancelledError:
            return
        except Exception as exc:
            self._set_task(task_id, "failed", error=str(exc))
            self._event(task_id, "task_error", {"message": str(exc)})
            self._sync_task_node(task_id)

    def _runnable_items(self, task: Dict[str, Any]) -> List[Dict[str, Any]]:
        statuses = {self._short_id(task["id"], item["id"]): item["status"] for item in task["items"]}
        stage_statuses = {stage["id"].split(":", 1)[-1]: stage["status"] for stage in task["stages"]}
        reviewed = self._reviewed_stages(task)
        gated_stages = {stage["spec"].get("id") for stage in task["stages"] if self._stage_needs_checkpoint(stage)}
        queued: List[Dict[str, Any]] = []
        retries: List[Dict[str, Any]] = []
        now = _now_ms()
        for item in task["items"]:
            if item["status"] not in RUNNABLE_ITEM_STATES:
                continue
            if item["status"] == "retrying" and int(item.get("retry_after") or 0) > now:
                continue
            if all(statuses.get(dep) == "completed" for dep in item["depends_on"]):
                stage = next((row for row in task["stages"] if row["spec"].get("id") == item["stage_id"]), None)
                dependencies_reviewed = all(dep not in gated_stages or dep in reviewed for dep in (stage or {}).get("depends_on", []))
                if stage and dependencies_reviewed and all(stage_statuses.get(dep) == "completed" for dep in stage["depends_on"]):
                    (queued if item["status"] == "queued" else retries).append(item)
        global_limit = max(1, int(task["spec"].get("concurrency", {}).get("global") or self.config().get("global") or 4))
        return [*queued, *retries][:global_limit]

    async def _execute_item(self, task_id: str, item: Dict[str, Any]) -> None:
        task = self.get(task_id, include_items=False)
        stage = next(row for row in task["stages"] if row["spec"].get("id") == item["stage_id"])
        provider = str(item["payload"].get("provider_id") or "unknown")
        kind = stage["type"]
        sem = self._provider_semaphore(provider, kind, task["spec"])
        async with self._global_sem, sem:
            latest = self.get(task_id)
            current = next(row for row in latest["items"] if row["id"] == item["id"])
            if latest["status"] in {"paused", "cancelled"} or current["status"] not in RUNNABLE_ITEM_STATES:
                return
            if current["depends_on"]:
                canvas = self.load_canvas(latest["canvas_id"])
                live_nodes = {str(node.get("id") or ""): node for node in canvas.get("nodes") or [] if isinstance(node, dict)}
                by_short = {self._short_id(task_id, row["id"]): row for row in latest["items"]}
                missing = []
                for dep in current["depends_on"]:
                    dep_row = by_short.get(dep, {})
                    dep_node_id = str(dep_row.get("node_id") or "")
                    if not dep_node_id:
                        continue
                    live = live_nodes.get(dep_node_id)
                    if not live or str(live.get("complexTaskId") or "") != task_id or str(live.get("complexTaskItemId") or "") != dep:
                        missing.append(dep)
                if missing:
                    self._update_item(current["id"], status="blocked", error="依赖节点已被删除")
                    self._set_task(task_id, "waiting_user", question=f"{current['title']} 的依赖节点已被删除，请决定重建或跳过")
                    self._event(task_id, "revision_conflict", {"item_id": self._short_id(task_id, current["id"]), "missing_dependencies": missing})
                    self._sync_task_node(task_id)
                    return
            if current.get("node_id"):
                canvas = self.load_canvas(latest["canvas_id"])
                live = next((node for node in canvas.get("nodes") or [] if str(node.get("id") or "") == str(current["node_id"])), None)
                if not live or str(live.get("complexTaskId") or "") != task_id or str(live.get("complexTaskItemId") or "") != self._short_id(task_id, current["id"]):
                    self._update_item(current["id"], status="blocked", error="任务节点已被删除或替换")
                    self._set_task(task_id, "waiting_user", question=f"{current['title']} 的任务节点已被删除或替换，请决定重建或跳过")
                    self._event(task_id, "revision_conflict", {"item_id": self._short_id(task_id, current["id"]), "reason": "owned_node_missing_or_replaced"})
                    self._sync_task_node(task_id)
                    return
            attempts_limit = int(latest["spec"]["limits"]["max_total_attempts"])
            generation_stages = {row["spec"].get("id") for row in latest["stages"] if row["type"] in {"generate_image", "generate_video"}}
            provider_attempts = sum(row["attempt_count"] for row in latest["items"] if row["stage_id"] in generation_stages)
            if kind in {"generate_image", "generate_video"} and provider_attempts >= attempts_limit:
                self._set_task(task_id, "waiting_user", question="任务已达到最大 Provider 尝试次数")
                return
            attempt = int(current["attempt_count"] or 0) + 1
            attempt_id = uuid.uuid4().hex
            attempt_started_at = _now_ms()
            execution_payload = self._execution_payload(latest, current, kind)
            circuit_key = f"{kind}:{provider}"
            circuit_until = float(self._provider_circuit_until.get(circuit_key) or 0)
            if circuit_until > time.monotonic():
                retry_after = _now_ms() + max(100, int((circuit_until - time.monotonic()) * 1000))
                self._update_item(current["id"], status="retrying", retry_after=retry_after, error="Provider 连续失败，熔断等待中")
                self._refresh(task_id)
                return
            self._update_item(current["id"], status="submitting", retry_after=0, attempt_count=attempt, error="")
            self._insert_attempt(attempt_id, task_id, current["id"], attempt, execution_payload)
            self._refresh(task_id)
            try:
                if kind in {"generate_image", "generate_video"}:
                    node_id = current["node_id"] or self._ensure_item_node(latest, stage, current)
                    self._update_item(current["id"], node_id=node_id, status="running")
                    self._refresh(task_id)
                    submitted = await self.submit_generation(task_id, kind, execution_payload, node_id)
                    provider_task_id = str(submitted.get("task_id") or "")
                    if not provider_task_id:
                        raise ComplexTaskError("Provider 未返回任务 ID")
                    self._update_item(current["id"], provider_task_id=provider_task_id, status="waiting_provider")
                    self._update_attempt(attempt_id, status="waiting_provider", provider_task_id=provider_task_id)
                    self._refresh(task_id)
                    result = await self.poll_generation(provider_task_id)
                    status = str(result.get("status") or "")
                    if status != "succeeded":
                        raise ComplexTaskError(str(result.get("error") or f"Provider 任务状态：{status}"))
                    self._update_item(current["id"], status="completed", retry_after=0, result_json=_json(result), error="")
                    self._update_attempt(attempt_id, status="completed", result_json=_json(result))
                    self._apply_generation_result(task_id, current["id"], node_id, kind, result, run_ms=_now_ms() - attempt_started_at)
                    self._provider_failures[circuit_key] = 0
                elif kind == "review":
                    self._update_item(current["id"], status="completed", result_json=_json({"checkpoint": True}))
                    self._update_attempt(attempt_id, status="completed", result_json=_json({"checkpoint": True}))
                else:
                    node_id = current["node_id"] or self._ensure_item_node(latest, stage, current)
                    self._update_item(current["id"], node_id=node_id, status="completed", result_json=_json({"node_id": node_id}))
                    self._update_attempt(attempt_id, status="completed", result_json=_json({"node_id": node_id}))
                self._event(task_id, "item_completed", {"item_id": self._short_id(task_id, current["id"]), "stage": stage["spec"].get("id")})
                self._refresh(task_id)
            except Exception as exc:
                failures = int(self._provider_failures.get(circuit_key) or 0) + 1
                self._provider_failures[circuit_key] = failures
                if failures >= 3:
                    self._provider_circuit_until[circuit_key] = time.monotonic() + min(60.0, 2.0 ** min(6, failures - 1))
                retries = int(latest["spec"]["limits"]["max_retries_per_item"])
                next_status = "retrying" if attempt <= retries else "failed"
                delay = min(8.0, 2.0 ** max(0, attempt - 1))
                retry_after = _now_ms() + int((delay + random.uniform(0, min(1.0, delay * 0.25))) * 1000) if next_status == "retrying" else 0
                self._update_item(current["id"], status=next_status, retry_after=retry_after, error=str(exc))
                self._update_attempt(attempt_id, status="failed", error=str(exc))
                self._event(task_id, "item_failed", {"item_id": self._short_id(task_id, current["id"]), "attempt": attempt, "retrying": next_status == "retrying", "error": str(exc)})
                if kind in {"generate_image", "generate_video"}:
                    self._apply_generation_failure(
                        task_id, current["id"], str(exc), terminal=next_status == "failed",
                        run_ms=_now_ms() - attempt_started_at,
                    )
                self._refresh(task_id)

    def _execution_payload(self, task: Dict[str, Any], item: Dict[str, Any], kind: str) -> Dict[str, Any]:
        payload = dict(item.get("payload") or {})
        by_short = {self._short_id(task["id"], row["id"]): row for row in task.get("items") or []}
        prompts: List[str] = []
        references: List[Dict[str, Any]] = []
        for dep_id in item.get("depends_on") or []:
            dep = by_short.get(dep_id) or {}
            dep_payload = dep.get("payload") if isinstance(dep.get("payload"), dict) else {}
            prompt = str(dep_payload.get("text") or dep_payload.get("prompt") or "").strip()
            if prompt:
                prompts.append(prompt)
            result = dep.get("result") if isinstance(dep.get("result"), dict) else {}
            media = result.get("image_items") or result.get("images") or []
            for raw in media:
                value = {"url": raw} if isinstance(raw, str) else dict(raw or {})
                if value.get("url"):
                    references.append({"url": value["url"], "name": value.get("name") or "upstream.png", "kind": "image"})
        if not str(payload.get("prompt") or "").strip() and prompts:
            payload["prompt"] = "\n\n".join(prompts)
        if kind in {"generate_image", "generate_video"} and references:
            existing = payload.get("reference_images") if isinstance(payload.get("reference_images"), list) else []
            payload["reference_images"] = [*existing, *references][:8]
        return payload

    async def _recover_provider_item(self, task_id: str, item: Dict[str, Any]) -> None:
        """Resume polling a paid request without submitting it a second time."""
        provider_task_id = str(item.get("provider_task_id") or "")
        if not provider_task_id:
            return
        task = self.get(task_id)
        stage = next((row for row in task["stages"] if row["spec"].get("id") == item["stage_id"]), None)
        if not stage:
            self._update_item(item["id"], status="interrupted", error="恢复时找不到所属阶段")
            return
        try:
            result = await self.poll_generation(provider_task_id)
            status = str(result.get("status") or "")
            if status != "succeeded":
                if status in {"queued", "running", "pending", "waiting_provider"}:
                    return
                raise ComplexTaskError(str(result.get("error") or f"Provider 任务状态：{status}"))
            node_id = str(item.get("node_id") or "")
            self._update_item(item["id"], status="completed", result_json=_json(result), error="")
            if node_id:
                self._apply_generation_result(task_id, item["id"], node_id, stage["type"], result)
            self._event(task_id, "item_recovered", {"item_id": self._short_id(task_id, item["id"]), "provider_task_id": provider_task_id})
        except Exception as exc:
            self._update_item(item["id"], status="failed", error=str(exc))
            self._event(task_id, "item_recovery_failed", {"item_id": self._short_id(task_id, item["id"]), "error": str(exc)})

    def _provider_semaphore(self, provider: str, kind: str, spec: Dict[str, Any]) -> asyncio.Semaphore:
        configured = spec.get("concurrency") if isinstance(spec.get("concurrency"), dict) else {}
        providers = self.config().get("providers") if isinstance(self.config().get("providers"), dict) else {}
        if provider in configured:
            limit = int(configured[provider])
        elif provider in providers:
            limit = int(providers[provider])
        elif kind == "generate_video":
            limit = int(self.config().get("video") or 1)
        elif any(marker in provider.lower() for marker in ("gpt", "openai", "codex")):
            limit = int(self.config().get("gpt_image") or 3)
        else:
            limit = int(self.config().get("unknown_image") or 1)
        key = f"{kind}:{provider}:{max(1, limit)}"
        if key not in self._provider_sems:
            self._provider_sems[key] = asyncio.Semaphore(max(1, limit))
        return self._provider_sems[key]

    @staticmethod
    def _generation_node_metadata(payload: Dict[str, Any], kind: str) -> Dict[str, Any]:
        prompt = str(payload.get("prompt") or payload.get("text") or "")
        refs = []
        for index, raw in enumerate(payload.get("reference_images") or []):
            value = {"url": raw} if isinstance(raw, str) else dict(raw or {})
            if not value.get("url"):
                continue
            refs.append({
                "url": value["url"],
                "displayUrl": _reference_display_url(value["url"]),
                "name": value.get("name") or f"图{index + 1}",
                "kind": value.get("kind") or "image",
                "nodeId": value.get("nodeId") or value.get("node_id") or "",
                "imageIndex": value.get("imageIndex", value.get("image_index", "")),
            })
        provider_id = str(payload.get("provider_id") or "")
        model = str(payload.get("model") or "")
        is_video = kind == "generate_video"
        settings: Dict[str, Any] = {
            "engine": "api",
            "apiKind": "video" if is_video else "image",
            "provider_id": provider_id,
            "model": model,
        }
        if is_video:
            settings.update({
                "videoProvider": provider_id,
                "videoModel": model,
                "videoDuration": max(1, min(60, int(payload.get("duration") or 5))),
                "videoAspect": str(payload.get("aspect_ratio") or payload.get("aspect") or "16:9"),
                "videoResolution": str(payload.get("resolution") or ""),
                "videoCameraFixed": bool(payload.get("camerafixed", payload.get("camera_fixed", False))),
                "videoGenerateAudio": bool(payload.get("generate_audio", False)),
            })
        else:
            settings.update({
                "count": max(1, min(8, int(payload.get("count") or payload.get("n") or 1))),
                "ratio": str(payload.get("ratio") or "square"),
                "resolution": str(payload.get("resolution") or "1k"),
                "customRatio": str(payload.get("aspect_ratio") or ""),
                "customSize": str(payload.get("size") or "1024x1024"),
                "quality": str(payload.get("quality") or "auto"),
            })
        return {
            "agentGenerated": True,
            "outputKind": "video" if is_video else "image",
            "runPrompt": prompt,
            "runModelPrompt": prompt,
            "runPromptRefs": refs,
            "runInputRefs": refs,
            "runSettings": settings,
            "runAt": _now_ms(),
        }

    def _ensure_item_node(self, task: Dict[str, Any], stage: Dict[str, Any], item: Dict[str, Any]) -> str:
        canvas = self.load_canvas(task["canvas_id"])
        nodes = canvas.setdefault("nodes", [])
        task_node_id = str(task["summary"].get("task_node_id") or "")
        task_node = next((node for node in nodes if str(node.get("id")) == task_node_id), None)
        stage_order = int(stage["stage_order"])
        x = float((task_node or {}).get("x") or 0) + 410 * (stage_order + 1)
        y = float((task_node or {}).get("y") or 0) + int(item["item_order"]) * 250
        payload = item["payload"]
        node_id = f"tasknode_{uuid.uuid4().hex[:16]}"
        ntype = stage["type"]
        if ntype == "prompt" or (ntype == "canvas_operation" and str(payload.get("operation") or "prompt") in {"prompt", "text"}):
            node = {"id": node_id, "type": "smart-prompt", "x": x, "y": y, "w": 316, "h": 240, "title": str(payload.get("title") or item["title"]), "text": str(payload.get("text") or payload.get("prompt") or ""), "created_at": _now_ms()}
        elif ntype == "canvas_operation" and str(payload.get("operation") or "") == "media":
            media = payload.get("items") if isinstance(payload.get("items"), list) else []
            node = {"id": node_id, "type": "smart-image", "x": x, "y": y, "title": item["title"], "images": media, "scale": 0.5, "created_at": _now_ms()}
        else:
            node = {"id": node_id, "type": "smart-image", "x": x, "y": y, "title": "Video" if ntype == "generate_video" else "Image", "images": [], "pending": 1 if ntype.startswith("generate_") else 0, "running": ntype.startswith("generate_"), "created_at": _now_ms()}
            if ntype in {"generate_image", "generate_video"}:
                node.update(self._generation_node_metadata(payload, ntype))
        node.update({"complexTaskId": task["id"], "complexTaskItemId": self._short_id(task["id"], item["id"]), "complexTaskStageId": stage["spec"].get("id")})
        nodes.append(node)
        connections = canvas.setdefault("connections", [])
        dep_node_ids = []
        if item["depends_on"]:
            dep_rows = {self._short_id(task["id"], row["id"]): row for row in task["items"]}
            dep_node_ids = [dep_rows[value]["node_id"] for value in item["depends_on"] if value in dep_rows and dep_rows[value]["node_id"]]
        if not dep_node_ids and task_node_id:
            dep_node_ids = [task_node_id]
        for from_id in dep_node_ids:
            if not any(str(conn.get("from")) == from_id and str(conn.get("to")) == node_id and str(conn.get("kind") or "flow") == "flow" for conn in connections):
                connections.append({"from": from_id, "to": node_id, "kind": "flow"})
        self.save_canvas(canvas)
        self._broadcast(task["canvas_id"], canvas)
        return node_id

    def _arrange_task_nodes(self, task_id: str) -> int:
        """Compact task-owned nodes after execution using their final display sizes."""
        task = self.get(task_id)
        canvas = self.load_canvas(task["canvas_id"])
        nodes = canvas.get("nodes") or []
        task_node_id = str(task["summary"].get("task_node_id") or "")
        task_node = next((node for node in nodes if str(node.get("id") or "") == task_node_id), None)
        if not task_node:
            return 0
        owned = {
            str(node.get("complexTaskItemId") or ""): node
            for node in nodes
            if str(node.get("complexTaskId") or "") == task_id
        }
        if not owned:
            return 0
        stage_by_id = {str(stage["spec"].get("id") or ""): stage for stage in task["stages"]}
        start_x = float(task_node.get("x") or 0) + max(24.0, float(task_node.get("w") or 316)) + 120.0
        start_y = float(task_node.get("y") or 0)
        column_x = start_x
        arranged = 0
        for stage in sorted(task["stages"], key=lambda row: int(row.get("stage_order") or 0)):
            stage_id = str(stage["spec"].get("id") or "")
            stage_items = sorted(
                (item for item in task["items"] if str(item.get("stage_id") or "") == stage_id),
                key=lambda row: int(row.get("item_order") or 0),
            )
            column_nodes = []
            for item in stage_items:
                short_id = self._short_id(task_id, str(item.get("id") or ""))
                node = owned.get(short_id)
                if node:
                    column_nodes.append((item, node))
            if not column_nodes:
                continue
            y = start_y
            column_width = 0.0
            for item, node in column_nodes:
                width = float(node.get("w") or 0)
                height = float(node.get("h") or 0)
                if width <= 24 or height <= 24:
                    stage_type = str(stage_by_id.get(stage_id, {}).get("type") or "")
                    if stage_type in {"generate_image", "generate_video"}:
                        planned_w, planned_h = _planned_media_node_size(item.get("payload") or {}, stage_type)
                        width, height = float(planned_w), float(planned_h)
                        node["w"], node["h"] = planned_w, planned_h
                    else:
                        width, height = 316.0, 240.0
                        node.setdefault("w", 316)
                        node.setdefault("h", 240)
                node["x"] = round(column_x)
                node["y"] = round(y)
                y += height + 48.0
                column_width = max(column_width, width)
                arranged += 1
            column_x += column_width + 120.0
        if arranged:
            self.save_canvas(canvas)
            self._broadcast(task["canvas_id"], canvas)
        return arranged

    def _create_task_node(self, task_id: str, summary: Dict[str, Any]) -> None:
        task = self.get(task_id, include_items=False)
        canvas = self.load_canvas(task["canvas_id"])
        nodes = canvas.setdefault("nodes", [])
        right = max((float(node.get("x") or 0) + float(node.get("w") or 320) for node in nodes), default=0)
        node = {
            "id": summary["task_node_id"], "type": "smart-agent-task", "x": right + 120, "y": min((float(node.get("y") or 0) for node in nodes), default=0),
            "w": 316, "h": 240, "title": task["title"], "complexTaskId": task_id, "taskStatus": task["status"],
            "taskMode": task["mode"], "taskProgress": summary, "linkVisibility": task["link_visibility"], "created_at": _now_ms(),
        }
        nodes.append(node)
        self.save_canvas(canvas)
        self._broadcast(task["canvas_id"], canvas)

    def _append_canvas_generation_log(
        self, canvas: Dict[str, Any], task: Dict[str, Any], item: Dict[str, Any], node: Dict[str, Any],
        kind: str, *, outputs: List[Dict[str, Any]], error: str = "", run_ms: int = 0,
    ) -> None:
        """Write batch results into the same canvas.logs schema used by manual generation."""
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        provider_id = str(payload.get("provider_id") or "")
        model = str(payload.get("model") or "")
        is_video = kind == "generate_video"
        request = {
            "provider_id": provider_id,
            "model": model,
            "task_id": str(item.get("provider_task_id") or ""),
        }
        if is_video:
            request.update({
                "duration": payload.get("duration") or 5,
                "aspect_ratio": payload.get("aspect_ratio") or payload.get("aspect") or "16:9",
                "resolution": payload.get("resolution") or "",
            })
        else:
            request.update({
                "size": payload.get("size") or "",
                "quality": payload.get("quality") or "auto",
                "n": payload.get("count") or payload.get("n") or 1,
            })
        attempt_no = max(1, int(item.get("attempt_count") or 1))
        status = "failed" if error else "success"
        short_item_id = self._short_id(task["id"], str(item.get("id") or ""))
        log_id = f"batchlog_{task['id']}_{short_item_id}_{attempt_no}_{status}"
        entry = {
            "id": log_id,
            "createdAt": _now_ms(),
            "status": status,
            "platform": provider_id or ("Video" if is_video else "API"),
            "nodeId": str(node.get("id") or ""),
            "nodeType": str(node.get("type") or "smart-image"),
            "model": model or ("Video" if is_video else "API Image"),
            "request": request,
            "prompt": str(payload.get("prompt") or payload.get("text") or ""),
            "outputs": outputs,
            "refs": list(node.get("runInputRefs") or node.get("runPromptRefs") or []),
            "runMs": max(0, int(run_ms or 0)),
            "error": str(error or ""),
            "batchTaskId": task["id"],
            "batchTaskItemId": short_item_id,
        }
        logs = canvas.get("logs") if isinstance(canvas.get("logs"), list) else []
        canvas["logs"] = [entry, *(row for row in logs if str((row or {}).get("id") or "") != log_id)][:500]

    def _apply_generation_result(
        self, task_id: str, item_id: str, node_id: str, kind: str, result: Dict[str, Any], *, run_ms: int = 0,
    ) -> None:
        task = self.get(task_id)
        item = next((row for row in task.get("items") or [] if str(row.get("id")) == str(item_id)), None)
        canvas = self.load_canvas(task["canvas_id"])
        node = next((row for row in canvas.get("nodes") or [] if str(row.get("id")) == node_id), None)
        if not node or not item:
            return
        raw_items = result.get("image_items") if kind == "generate_image" else result.get("video_items")
        if not isinstance(raw_items, list):
            raw_items = result.get("images") if kind == "generate_image" else result.get("videos")
        media = []
        for index, raw in enumerate(raw_items or []):
            value = {"url": raw} if isinstance(raw, str) else dict(raw or {})
            if value.get("url"):
                output = {"url": value["url"], "name": value.get("name") or f"output-{index + 1}.{'png' if kind == 'generate_image' else 'mp4'}", "kind": "image" if kind == "generate_image" else "video", "generatedResult": True}
                for key in ("natural_w", "natural_h", "width", "height", "w", "h", "layout_w", "layout_h", "poster"):
                    if value.get(key) not in (None, ""):
                        output[key] = value[key]
                media.append(output)
        node["images"] = media
        node["pending"] = 0
        node["running"] = False
        node.pop("queued", None)
        node.pop("generationError", None)
        node["outputKind"] = "image" if kind == "generate_image" else "video"
        node["title"] = "Video" if kind == "generate_video" else ("Group" if len(media) > 1 else "Image")
        node.pop("pendingTasks", None)
        self._append_canvas_generation_log(canvas, task, item, node, kind, outputs=media, run_ms=run_ms)
        self.save_canvas(canvas)
        self._broadcast(task["canvas_id"], canvas)

    def _apply_generation_failure(
        self, task_id: str, item_id: str, error: str, *, terminal: bool, run_ms: int = 0,
    ) -> None:
        task = self.get(task_id)
        item = next((row for row in task.get("items") or [] if str(row.get("id")) == str(item_id)), None)
        node_id = str((item or {}).get("node_id") or "")
        if not node_id:
            return
        canvas = self.load_canvas(task["canvas_id"])
        node = next((row for row in canvas.get("nodes") or [] if str(row.get("id")) == node_id), None)
        if not node:
            return
        node["pending"] = 0
        node["running"] = False
        node.pop("pendingTasks", None)
        if terminal:
            node.pop("queued", None)
            node["generationError"] = {"message": str(error or "生成任务失败")[:2000], "kind": node.get("outputKind") or "image"}
            self._append_canvas_generation_log(
                canvas, task, item, node,
                "generate_video" if node.get("outputKind") == "video" else "generate_image",
                outputs=[], error=str(error or "生成任务失败"), run_ms=run_ms,
            )
        else:
            node["queued"] = True
            node.pop("generationError", None)
        self.save_canvas(canvas)
        self._broadcast(task["canvas_id"], canvas)

    async def _checkpoint(self, task_id: str, stage_id: str) -> str:
        task = self.get(task_id)
        maximum = int(task["spec"]["limits"]["max_agent_checkpoints"])
        if int(task["checkpoint_count"] or 0) >= maximum:
            self._set_task(task_id, "waiting_user", question="任务已达到最大子 Agent 检查次数")
            return "ask_user"
        self._set_task(task_id, "reviewing")
        self._sync_task_node(task_id)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("UPDATE complex_tasks SET checkpoint_count=checkpoint_count+1, updated_at=? WHERE id=?", (_now_ms(), task_id))
                conn.commit()
            finally:
                conn.close()
        if not self.run_checkpoint:
            self._set_task(task_id, "waiting_user", question="任务需要内容验收，但任务 Agent 当前不可用")
            return "ask_user"
        reason = "final_review" if stage_id == "__final__" else "stage_complete"
        review = await self.run_checkpoint(self.get(task_id), {"reason": reason, "stage_id": stage_id})
        if not isinstance(review, dict) or str(review.get("decision") or "") not in REVIEW_DECISIONS:
            self._set_task(task_id, "waiting_user", question="任务 Agent 未返回有效验收结果")
            return "ask_user"
        decision = str(review.get("decision") or "")
        self.submit_review(task_id, review)
        return decision

    @staticmethod
    def _stage_needs_checkpoint(stage: Dict[str, Any]) -> bool:
        spec = stage.get("spec") if isinstance(stage.get("spec"), dict) else {}
        acceptance = spec.get("acceptance") if isinstance(spec.get("acceptance"), dict) else {}
        return stage.get("type") == "review" or str(acceptance.get("review") or "").lower() == "agent" or bool(spec.get("agent_checkpoint"))

    @staticmethod
    def _reviewed_stages(task: Dict[str, Any]) -> Set[str]:
        runtime = task["spec"].get("runtime") if isinstance(task["spec"].get("runtime"), dict) else {}
        return {str(value) for value in runtime.get("reviewed_stages") or [] if str(value)}

    def _next_checkpoint_stage(self, task: Dict[str, Any]) -> str:
        if task.get("mode") != "agentic":
            return ""
        reviewed = self._reviewed_stages(task)
        for stage in sorted(task.get("stages") or [], key=lambda row: int(row.get("stage_order") or 0)):
            stage_id = str(stage.get("spec", {}).get("id") or "")
            if stage_id and stage_id not in reviewed and stage.get("status") == "completed" and self._stage_needs_checkpoint(stage):
                return stage_id
        return ""

    def _needs_final_checkpoint(self, task: Dict[str, Any]) -> bool:
        return task.get("mode") == "agentic" and "__final__" not in self._reviewed_stages(task)

    def _mark_checkpoint_done(self, task_id: str, stage_id: str) -> None:
        task = self.get(task_id, include_items=False)
        spec = task["spec"]
        runtime = spec.setdefault("runtime", {})
        reviewed = {str(value) for value in runtime.get("reviewed_stages") or [] if str(value)}
        reviewed.add(stage_id)
        runtime["reviewed_stages"] = sorted(reviewed)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("UPDATE complex_tasks SET spec_json=?, updated_at=? WHERE id=?", (_json(spec), _now_ms(), task_id))
                conn.commit()
            finally:
                conn.close()

    def _refresh(self, task_id: str) -> None:
        task = self.get(task_id)
        counts: Dict[str, int] = {}
        stage_updates: Dict[str, str] = {}
        for item in task["items"]:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        for stage in task["stages"]:
            stage_items = [item for item in task["items"] if item["stage_id"] == stage["spec"].get("id")]
            status = "completed" if stage_items and all(item["status"] == "completed" for item in stage_items) else "failed" if stage_items and all(item["status"] in TERMINAL_ITEM_STATES for item in stage_items) and any(item["status"] != "completed" for item in stage_items) else "running" if any(item["status"] not in {"queued"} | TERMINAL_ITEM_STATES for item in stage_items) else "queued"
            stage_updates[str(stage["id"])] = status
            with self._lock:
                conn = self._connect()
                try:
                    conn.execute("UPDATE complex_task_stages SET status=?, updated_at=? WHERE id=?", (status, _now_ms(), stage["id"]))
                    conn.commit()
                finally:
                    conn.close()
        active_stage = next((stage["title"] for stage in task["stages"] if stage_updates.get(str(stage["id"]), stage["status"]) != "completed"), task["stages"][-1]["title"] if task["stages"] else "")
        latest_event = task["events"][-1] if task.get("events") else {}
        latest_payload = latest_event.get("payload") if isinstance(latest_event.get("payload"), dict) else {}
        event_labels = {
            "task_created": "任务已创建", "item_completed": "项目已完成", "item_failed": "项目执行失败",
            "item_recovered": "已恢复 Provider 任务", "item_recovery_failed": "Provider 任务恢复失败",
            "agent_review": "子 Agent 已提交验收", "user_reply": "已收到用户回复",
            "revision_conflict": "任务依赖发生变化", "task_finished": "任务执行结束", "task_error": "任务执行出错",
        }
        event_type = str(latest_event.get("type") or "")
        recent_event = str(latest_payload.get("message") or latest_payload.get("error") or event_labels.get(event_type) or event_type)[:240]
        completed = counts.get("completed", 0)
        failed = counts.get("failed", 0) + counts.get("interrupted", 0) + counts.get("blocked", 0) + counts.get("cancelled", 0)
        running = counts.get("submitting", 0) + counts.get("running", 0) + counts.get("waiting_provider", 0)
        summary = {
            **task["summary"], "total": len(task["items"]), "processed": completed + failed, "completed": completed, "failed": failed,
            "running": running, "retrying": counts.get("retrying", 0), "waiting": counts.get("queued", 0), "current_stage": active_stage,
            "recent_event": recent_event,
        }
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("UPDATE complex_tasks SET summary_json=?, updated_at=? WHERE id=?", (_json(summary), _now_ms(), task_id))
                conn.commit()
            finally:
                conn.close()
        self._sync_task_node(task_id)

    def _sync_task_node(self, task_id: str) -> None:
        task = self.get(task_id, include_items=False)
        canvas = self.load_canvas(task["canvas_id"])
        node_id = str(task["summary"].get("task_node_id") or "")
        node = next((row for row in canvas.get("nodes") or [] if str(row.get("id")) == node_id), None)
        if not node:
            if task["status"] not in TERMINAL_TASK_STATES:
                self._cancel(task_id, detached=True)
            return
        node.update({"taskStatus": task["status"], "taskMode": task["mode"], "taskProgress": task["summary"], "taskQuestion": task["question"], "taskError": task["error"], "linkVisibility": task["link_visibility"]})
        self.save_canvas(canvas)
        self._broadcast(task["canvas_id"], canvas)

    def _broadcast(self, canvas_id: str, canvas: Dict[str, Any]) -> None:
        try:
            asyncio.get_running_loop().create_task(self.broadcast_canvas(canvas_id, int(canvas.get("updated_at") or _now_ms())))
        except RuntimeError:
            pass

    def _cancel(self, task_id: str, detached: bool = False) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("UPDATE complex_task_items SET status='cancelled', updated_at=? WHERE task_id=? AND status IN ('queued','retrying','interrupted','blocked')", (_now_ms(), task_id))
                conn.execute("UPDATE complex_tasks SET status='cancelled', error=?, updated_at=? WHERE id=?", ("任务节点已删除" if detached else "用户取消", _now_ms(), task_id))
                conn.commit()
            finally:
                conn.close()

    def _set_task(self, task_id: str, status: str, **fields: Any) -> None:
        allowed = {"question", "error", "task_thread_id", "link_visibility"}
        assignments, values = ["status=?", "updated_at=?"], [status, _now_ms()]
        for key, value in fields.items():
            if key in allowed:
                assignments.append(f"{key}=?")
                values.append(value)
        values.append(task_id)
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(f"UPDATE complex_tasks SET {', '.join(assignments)} WHERE id=?", values)
                conn.commit()
            finally:
                conn.close()

    def _update_item(self, item_id: str, **fields: Any) -> None:
        allowed = {"status", "node_id", "provider_task_id", "attempt_count", "retry_after", "error", "result_json", "payload_json"}
        pairs, values = [], []
        for key, value in fields.items():
            if key in allowed:
                pairs.append(f"{key}=?")
                values.append(value)
        if not pairs:
            return
        pairs.append("updated_at=?")
        values.extend([_now_ms(), item_id])
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(f"UPDATE complex_task_items SET {', '.join(pairs)} WHERE id=?", values)
                conn.commit()
            finally:
                conn.close()

    def _insert_attempt(self, attempt_id: str, task_id: str, item_id: str, attempt_no: int, payload: Dict[str, Any]) -> None:
        now = _now_ms()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("INSERT INTO complex_task_attempts(id,task_id,item_id,attempt_no,status,payload_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (attempt_id, task_id, item_id, attempt_no, "submitting", _json(payload), now, now))
                conn.commit()
            finally:
                conn.close()

    def _update_attempt(self, attempt_id: str, **fields: Any) -> None:
        allowed = {"status", "provider_task_id", "result_json", "error"}
        pairs, values = [], []
        for key, value in fields.items():
            if key in allowed:
                pairs.append(f"{key}=?")
                values.append(value)
        pairs.append("updated_at=?")
        values.extend([_now_ms(), attempt_id])
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(f"UPDATE complex_task_attempts SET {', '.join(pairs)} WHERE id=?", values)
                conn.commit()
            finally:
                conn.close()

    def _event(self, task_id: str, event_type: str, payload: Dict[str, Any]) -> None:
        with self._lock:
            conn = self._connect()
            try:
                conn.execute("INSERT INTO complex_task_events(task_id,event_type,payload_json,created_at) VALUES(?,?,?,?)", (task_id, event_type, _json(payload), _now_ms()))
                conn.commit()
            finally:
                conn.close()

    @staticmethod
    def _short_id(task_id: str, value: str) -> str:
        prefix = task_id + ":"
        return str(value)[len(prefix):] if str(value).startswith(prefix) else str(value)


__all__ = [
    "ComplexTaskEngine", "ComplexTaskError", "normalize_complex_task_spec", "normalize_batch_task_spec",
    "infer_complex_task_mode", "ALLOWED_STAGE_TYPES", "REVIEW_DECISIONS",
]
