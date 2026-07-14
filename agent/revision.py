"""Canvas Agent 私有画布 Revision 存储。

不修改上游 Canvas JSON schema；通过画布内容指纹识别 Agent 之外的持久化修改。
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


class CanvasRevisionConflict(RuntimeError):
    def __init__(self, expected: int, current: int):
        super().__init__(f"canvas revision conflict: expected {expected}, current {current}")
        self.expected = expected
        self.current = current


def canvas_fingerprint(canvas: Mapping[str, Any]) -> str:
    """忽略时间戳、日志和视口等不影响 Agent 结构判断的字段。"""
    relevant = {
        "id": canvas.get("id"),
        "title": canvas.get("title") or canvas.get("name"),
        "kind": canvas.get("kind"),
        "nodes": canvas.get("nodes") if isinstance(canvas.get("nodes"), list) else [],
        "connections": canvas.get("connections") if isinstance(canvas.get("connections"), list) else [],
        "settings": canvas.get("settings") if isinstance(canvas.get("settings"), dict) else {},
    }
    raw = json.dumps(relevant, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


class CanvasRevisionStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self._lock = threading.RLock()

    def _path(self, canvas_id: str) -> Path:
        key = hashlib.sha256(str(canvas_id or "no-canvas").encode("utf-8", errors="replace")).hexdigest()[:24]
        return self.root / f"{key}.json"

    def _read(self, canvas_id: str) -> Optional[Dict[str, Any]]:
        path = self._path(canvas_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else None
        except (OSError, ValueError, TypeError):
            return None

    def _write(self, canvas_id: str, data: Mapping[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(canvas_id)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(dict(data), ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def observe(self, canvas_id: str, canvas: Mapping[str, Any]) -> Dict[str, Any]:
        """读取 Revision；如指纹已变，将外部/手动变更记为一次新 Revision。"""
        canvas_key = str(canvas_id or canvas.get("id") or "")
        fingerprint = canvas_fingerprint(canvas)
        with self._lock:
            state = self._read(canvas_key)
            if not state:
                state = {"canvas_id": canvas_key, "revision": 0, "fingerprint": fingerprint, "updated_at": int(time.time() * 1000)}
                self._write(canvas_key, state)
                return dict(state)
            revision = max(0, int(state.get("revision") or 0))
            if str(state.get("fingerprint") or "") != fingerprint:
                revision += 1
                state = {"canvas_id": canvas_key, "revision": revision, "fingerprint": fingerprint, "updated_at": int(time.time() * 1000)}
                self._write(canvas_key, state)
            return dict(state)

    def assert_expected(self, canvas_id: str, canvas: Mapping[str, Any], expected: Optional[int]) -> Dict[str, Any]:
        state = self.observe(canvas_id, canvas)
        if expected is not None and int(expected) != int(state["revision"]):
            raise CanvasRevisionConflict(int(expected), int(state["revision"]))
        return state
