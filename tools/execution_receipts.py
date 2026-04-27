"""Execution receipts — auditable record of delegated task execution.

Provides durable JSON receipt artifacts indexed in SQLite for
query, reconcile, and prune operations.
"""

import json
import logging
import os
import sqlite3
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _get_hermes_home() -> Path:
    """Resolve HERMES_HOME directory."""
    from hermes_constants import get_hermes_home

    return get_hermes_home()


def _get_receipts_dir() -> Path:
    """Get the durable receipts artifact directory under HERMES_HOME."""
    d = _get_hermes_home() / "artifacts" / "execution-receipts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _get_receipts_db() -> Path:
    """Get the SQLite ledger path next to receipt artifacts."""
    return _get_receipts_dir() / "receipts.sqlite3"


def _receipt_path(receipt_id: str) -> Path:
    """Return the JSON artifact path for a receipt id."""
    return _get_receipts_dir() / f"{receipt_id}.json"


def _fsync_dir(path: Path) -> None:
    """Best-effort fsync for a directory after atomic replacement."""
    if not hasattr(os, "O_DIRECTORY"):
        return
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write bytes to a path using a temp file in the same directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
            tmp_name = tmp.name
            tmp.write(data)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
        _fsync_dir(path.parent)
    finally:
        if tmp_name:
            try:
                Path(tmp_name).unlink(missing_ok=True)
            except OSError:
                pass


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically write UTF-8 text to a path."""
    _atomic_write_bytes(path, text.encode("utf-8"))


@dataclass
class ExecutionReceipt:
    """A single execution receipt."""

    receipt_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    task_id: str = ""
    timestamp: float = field(default_factory=time.time)
    status: str = "completed"  # completed, failed, timeout, cancelled
    duration_seconds: float = 0.0
    execution_path: str = "delegation"  # delegation, direct, work_order
    worker_mode: str = ""  # warm, cold, docker
    runtime_kind: str = ""  # terminal, docker, ssh
    runtime_id: str = ""
    runtime_reused: bool = False
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    files_modified: List[str] = field(default_factory=list)
    summary: str = ""
    error: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=2)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ExecutionReceipt":
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})

    def save(self) -> Path:
        """Persist receipt as an atomic JSON artifact."""
        path = _receipt_path(self.receipt_id)
        _atomic_write_text(path, self.to_json())
        return path


def _get_connection() -> sqlite3.Connection:
    """Get SQLite connection to the receipts ledger."""
    db_path = _get_receipts_db()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the receipts table if it doesn't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS receipts (
            receipt_id TEXT PRIMARY KEY,
            task_id TEXT,
            timestamp REAL,
            status TEXT,
            duration_seconds REAL,
            execution_path TEXT,
            worker_mode TEXT,
            runtime_kind TEXT,
            runtime_id TEXT,
            runtime_reused INTEGER,
            files_modified TEXT,  -- JSON array
            summary TEXT,
            error TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_receipts_task_id ON receipts(task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_receipts_timestamp ON receipts(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_receipts_status ON receipts(status)")
    conn.commit()


def index_receipt(receipt: ExecutionReceipt) -> None:
    """Add or update a receipt in the SQLite ledger."""
    conn = _get_connection()
    try:
        conn.execute("""
            INSERT OR REPLACE INTO receipts
            (receipt_id, task_id, timestamp, status, duration_seconds,
             execution_path, worker_mode, runtime_kind, runtime_id,
             runtime_reused, files_modified, summary, error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            receipt.receipt_id,
            receipt.task_id,
            receipt.timestamp,
            receipt.status,
            receipt.duration_seconds,
            receipt.execution_path,
            receipt.worker_mode,
            receipt.runtime_kind,
            receipt.runtime_id,
            int(receipt.runtime_reused),
            json.dumps(receipt.files_modified),
            receipt.summary[:500],
            receipt.error[:500],
        ))
        conn.commit()
    finally:
        conn.close()


def _persist_and_index_receipt(receipt: ExecutionReceipt) -> Path:
    """Persist a receipt JSON artifact and index it, rolling back on index failure."""
    path = _receipt_path(receipt.receipt_id)
    existed = path.exists()
    previous = path.read_bytes() if existed else None

    receipt.save()
    try:
        index_receipt(receipt)
    except Exception:
        if previous is not None:
            _atomic_write_bytes(path, previous)
        else:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                logger.warning("Failed to remove unindexed receipt artifact %s", path)
        raise
    return path


def create_receipt(
    task_id: str = "",
    execution_path: str = "delegation",
    worker_mode: str = "",
    runtime_kind: str = "",
    runtime_id: str = "",
    metadata: Optional[Dict[str, Any]] = None,
) -> ExecutionReceipt:
    """Create and persist a new execution receipt."""
    receipt = ExecutionReceipt(
        task_id=task_id,
        execution_path=execution_path,
        worker_mode=worker_mode,
        runtime_kind=runtime_kind,
        runtime_id=runtime_id,
        metadata=metadata or {},
    )
    _persist_and_index_receipt(receipt)
    logger.debug("Created execution receipt %s for task %s", receipt.receipt_id, task_id)
    return receipt


def finalize_receipt(
    receipt: ExecutionReceipt,
    status: str = "completed",
    duration_seconds: float = 0.0,
    tool_calls: Optional[List[Dict[str, Any]]] = None,
    files_modified: Optional[List[str]] = None,
    summary: str = "",
    error: str = "",
    runtime_reused: bool = False,
) -> ExecutionReceipt:
    """Update a receipt with final results and re-persist."""
    receipt.status = status
    receipt.duration_seconds = duration_seconds
    receipt.tool_calls = tool_calls or []
    receipt.files_modified = files_modified or []
    receipt.summary = summary[:500]
    receipt.error = error[:500]
    receipt.runtime_reused = runtime_reused
    _persist_and_index_receipt(receipt)
    return receipt


def get_receipt(receipt_id: str) -> Optional[ExecutionReceipt]:
    """Load a receipt by ID from JSON file."""
    path = _receipt_path(receipt_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return ExecutionReceipt.from_dict(data)
    except (json.JSONDecodeError, TypeError):
        return None


def list_receipts(limit: int = 20, task_id: str = "") -> List[Dict[str, Any]]:
    """List recent receipts from the SQLite ledger."""
    conn = _get_connection()
    try:
        if task_id:
            rows = conn.execute(
                "SELECT * FROM receipts WHERE task_id = ? ORDER BY timestamp DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM receipts ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def query_receipts(
    status: str = "",
    execution_path: str = "",
    since: float = 0.0,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """Query receipts with filters."""
    conn = _get_connection()
    try:
        conditions = []
        params: list = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if execution_path:
            conditions.append("execution_path = ?")
            params.append(execution_path)
        if since > 0:
            conditions.append("timestamp >= ?")
            params.append(since)

        where = " AND ".join(conditions) if conditions else "1=1"
        params.append(limit)
        rows = conn.execute(
            f"SELECT * FROM receipts WHERE {where} ORDER BY timestamp DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def prune_receipts(
    older_than_hours: float = 72.0,
    keep_min: int = 10,
    keep_failed: bool = True,
) -> Dict[str, Any]:
    """Remove old receipts, keeping at least keep_min most recent.

    Failed receipts are kept by default because they are usually the most useful
    audit records during debugging.
    """
    if older_than_hours <= 0:
        raise ValueError("older_than_hours must be positive")

    conn = _get_connection()
    try:
        cutoff = time.time() - (older_than_hours * 3600)
        total = conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0]
        conditions = ["timestamp < ?"]
        params: list[Any] = [cutoff]
        if keep_failed:
            conditions.append("status != 'failed'")
        where = " AND ".join(conditions)
        old_rows = conn.execute(
            f"SELECT receipt_id FROM receipts WHERE {where} ORDER BY timestamp ASC",
            params,
        ).fetchall()

        can_prune = max(0, len(old_rows) - max(0, keep_min - (total - len(old_rows))))
        to_prune = [r["receipt_id"] for r in old_rows[:can_prune]]
        missing_files: list[str] = []

        if to_prune:
            placeholders = ",".join("?" * len(to_prune))
            conn.execute(f"DELETE FROM receipts WHERE receipt_id IN ({placeholders})", to_prune)
            conn.commit()
            receipts_dir = _get_receipts_dir()
            for rid in to_prune:
                f = receipts_dir / f"{rid}.json"
                if f.exists():
                    f.unlink()
                else:
                    missing_files.append(rid)

        return {
            "total_before": total,
            "pruned": len(to_prune),
            "remaining": total - len(to_prune),
            "deleted_receipt_ids": to_prune,
            "missing_files": missing_files,
            "cutoff": cutoff,
        }
    finally:
        conn.close()


def _scan_receipt_files(receipts_dir: Path) -> tuple[Dict[str, ExecutionReceipt], List[Dict[str, str]], int]:
    """Scan receipt JSON artifacts and return valid receipts plus parse errors."""
    valid: Dict[str, ExecutionReceipt] = {}
    parse_errors: List[Dict[str, str]] = []
    total_files = 0
    for path in receipts_dir.glob("*.json"):
        if not path.is_file():
            continue
        total_files += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            receipt = ExecutionReceipt.from_dict(data)
            if not receipt.receipt_id:
                raise ValueError("receipt_id is missing")
            if receipt.receipt_id != path.stem:
                raise ValueError(
                    f"receipt_id {receipt.receipt_id!r} does not match file stem {path.stem!r}"
                )
        except Exception as exc:
            parse_errors.append({
                "receipt_id": path.stem,
                "path": str(path),
                "error": str(exc),
            })
            continue
        valid[receipt.receipt_id] = receipt
    return valid, parse_errors, total_files


def reconcile_receipts() -> Dict[str, Any]:
    """Reconcile JSON files vs SQLite ledger — fix inconsistencies."""
    receipts_dir = _get_receipts_dir()
    valid_receipts, parse_errors, total_json_files = _scan_receipt_files(receipts_dir)
    conn = _get_connection()
    try:
        db_ids = {r["receipt_id"] for r in conn.execute("SELECT receipt_id FROM receipts").fetchall()}
        json_ids = set(valid_receipts)

        in_db_only = db_ids - json_ids - {e["receipt_id"] for e in parse_errors}
        in_files_only = json_ids - db_ids

        reindexed = 0
        reindexed_ids: list[str] = []
        for rid in sorted(in_files_only):
            index_receipt(valid_receipts[rid])
            reindexed += 1
            reindexed_ids.append(rid)

        removed_from_db = 0
        removed_ids: list[str] = []
        for rid in sorted(in_db_only):
            conn.execute("DELETE FROM receipts WHERE receipt_id = ?", (rid,))
            removed_from_db += 1
            removed_ids.append(rid)
        if removed_from_db:
            conn.commit()

        return {
            "json_files": total_json_files,
            "valid_json_files": len(valid_receipts),
            "db_entries": len(db_ids),
            "reindexed": reindexed,
            "reindexed_receipt_ids": reindexed_ids,
            "removed_from_db": removed_from_db,
            "removed_from_db_receipt_ids": removed_ids,
            "parse_errors": parse_errors,
            "consistent": not in_db_only and not in_files_only and not parse_errors,
        }
    finally:
        conn.close()


def maintenance_status() -> Dict[str, Any]:
    """Get current maintenance status of the receipts system without mutating it."""
    receipts_dir = _get_receipts_dir()
    valid_receipts, parse_errors, total_json_files = _scan_receipt_files(receipts_dir)
    conn = _get_connection()
    try:
        rows = conn.execute("SELECT receipt_id FROM receipts").fetchall()
        db_ids = {r["receipt_id"] for r in rows}
        json_ids = set(valid_receipts)
        missing_files = sorted(db_ids - json_ids - {e["receipt_id"] for e in parse_errors})
        unindexed_files = sorted(json_ids - db_ids)
        total_db = len(db_ids)
        db_size = _get_receipts_db().stat().st_size if _get_receipts_db().exists() else 0
        dir_size = sum(f.stat().st_size for f in receipts_dir.glob("*.json") if f.is_file())
        oldest = conn.execute("SELECT MIN(timestamp) FROM receipts").fetchone()[0]
        newest = conn.execute("SELECT MAX(timestamp) FROM receipts").fetchone()[0]

        return {
            "total_receipts": total_db,
            "json_files": total_json_files,
            "valid_json_files": len(valid_receipts),
            "db_size_bytes": db_size,
            "dir_size_bytes": dir_size,
            "oldest_timestamp": oldest,
            "newest_timestamp": newest,
            "missing_files": missing_files,
            "unindexed_files": unindexed_files,
            "parse_errors": parse_errors,
            "consistent": not missing_files and not unindexed_files and not parse_errors,
        }
    finally:
        conn.close()
