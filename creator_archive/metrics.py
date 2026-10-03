"""Additive local metrics schema and immutable observation snapshots."""
import json
from contextlib import closing
import math
from pathlib import Path
import sqlite3
import time

FIELDS = ("likes", "collects", "comments")


def migrate(db_path, *, backup_required=True):
    """Back up the complete SQLite state before the first additive migration."""
    with closing(sqlite3.connect(db_path)) as db:
        columns = {r[1] for r in db.execute("PRAGMA table_info(items)")}
        if "content_type" in columns:
            return None
        backup = None
        if backup_required:
            backups = Path(db_path).parent / "backups"
            backups.mkdir(exist_ok=True)
            backup = backups / f"archive-before-metrics-{time.time_ns()}.sqlite3"
            with closing(sqlite3.connect(backup)) as target:
                db.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("数据库备份校验失败，未执行升级")
        db.executescript("""
            BEGIN IMMEDIATE;
            ALTER TABLE items ADD COLUMN content_type TEXT NOT NULL DEFAULT 'unknown';
            ALTER TABLE items ADD COLUMN title TEXT;
            ALTER TABLE items ADD COLUMN media_state TEXT NOT NULL DEFAULT 'unknown';
            CREATE TABLE metric_snapshots (
                id INTEGER PRIMARY KEY, platform TEXT NOT NULL, item_id TEXT NOT NULL,
                observation_key TEXT NOT NULL, collected_at REAL NOT NULL,
                source TEXT NOT NULL, status TEXT NOT NULL, reason TEXT, metrics_json TEXT NOT NULL,
                UNIQUE(platform,item_id,observation_key));
            CREATE TABLE item_metrics (
                platform TEXT NOT NULL, item_id TEXT NOT NULL, field TEXT NOT NULL,
                value INTEGER, quality TEXT NOT NULL, raw TEXT, source TEXT, collected_at REAL,
                last_attempt_at REAL NOT NULL, last_attempt_status TEXT NOT NULL,
                PRIMARY KEY(platform,item_id,field));
            CREATE INDEX metric_sort ON item_metrics(field,value,platform,item_id);
            CREATE TABLE job_items (
                job_id INTEGER NOT NULL, platform TEXT NOT NULL, item_id TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'queued', reason TEXT,
                PRIMARY KEY(job_id,platform,item_id));
            PRAGMA user_version=2;
            COMMIT;
        """)
    return str(backup) if backup else None


def read_metrics(db, platform, item_id):
    result = {field: dict(value=None, quality="unknown", raw=None, source=None,
                         collected_at=None, last_attempt_at=None, last_attempt_status=None) for field in FIELDS}
    for row in db.execute("SELECT * FROM item_metrics WHERE platform=? AND item_id=?", (platform, item_id)):
        result[row["field"]] = {k: row[k] for k in result[row["field"]]}
    return result


def read_snapshots(db, platform, item_id):
    result = []
    for row in db.execute("SELECT id,collected_at,source,status,reason,metrics_json FROM metric_snapshots WHERE platform=? AND item_id=? ORDER BY collected_at DESC,id DESC", (platform, item_id)):
        value = dict(row)
        value["metrics"] = json.loads(value.pop("metrics_json"))
        result.append(value)
    return result


def save_observation(db, platform, item_id, metrics, *, source, collected_at, observation_key,
                     status="ok", reason=None):
    if not isinstance(source, str) or not source or len(source) > 120 or "://" in source:
        raise ValueError("invalid_metric_source")
    if not isinstance(collected_at, (int, float)) or not math.isfinite(collected_at) or collected_at <= 0:
        raise ValueError("invalid_metric_time")
    normalized = {}
    for field in FIELDS:
        raw = metrics.get(field, {}) if status == "ok" else {}
        value, quality = raw.get("value"), raw.get("quality", "unknown")
        if type(value) is not int or value < 0 or value > 9223372036854775807 or quality not in {"exact", "approximate", "lower_bound"}:
            value, quality = None, "unknown"
        normalized[field] = dict(value=value, quality=quality, raw=str(raw["raw"])[:80] if raw.get("raw") is not None else None)
    inserted = db.execute("INSERT OR IGNORE INTO metric_snapshots(platform,item_id,observation_key,collected_at,source,status,reason,metrics_json) VALUES(?,?,?,?,?,?,?,?)",
                         (platform,item_id,observation_key,collected_at,source,status,reason,json.dumps(normalized))).rowcount
    if not inserted:
        return False
    for field, metric in normalized.items():
        attempt_status = "failed" if status != "ok" else "missing" if metric["value"] is None else "ok"
        db.execute("""INSERT INTO item_metrics VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(platform,item_id,field) DO UPDATE SET
            value=CASE WHEN excluded.value IS NOT NULL AND (item_metrics.collected_at IS NULL OR excluded.collected_at>=item_metrics.collected_at) THEN excluded.value ELSE item_metrics.value END,
            quality=CASE WHEN excluded.value IS NOT NULL AND (item_metrics.collected_at IS NULL OR excluded.collected_at>=item_metrics.collected_at) THEN excluded.quality ELSE item_metrics.quality END,
            raw=CASE WHEN excluded.value IS NOT NULL AND (item_metrics.collected_at IS NULL OR excluded.collected_at>=item_metrics.collected_at) THEN excluded.raw ELSE item_metrics.raw END,
            source=CASE WHEN excluded.value IS NOT NULL AND (item_metrics.collected_at IS NULL OR excluded.collected_at>=item_metrics.collected_at) THEN excluded.source ELSE item_metrics.source END,
            collected_at=CASE WHEN excluded.value IS NOT NULL AND (item_metrics.collected_at IS NULL OR excluded.collected_at>=item_metrics.collected_at) THEN excluded.collected_at ELSE item_metrics.collected_at END,
            last_attempt_status=CASE WHEN excluded.last_attempt_at>=item_metrics.last_attempt_at THEN excluded.last_attempt_status ELSE item_metrics.last_attempt_status END,
            last_attempt_at=max(item_metrics.last_attempt_at,excluded.last_attempt_at)""",
            (platform,item_id,field,metric["value"],metric["quality"],metric["raw"],source if metric["value"] is not None else None,
             collected_at if metric["value"] is not None else None,collected_at,attempt_status))
    return True
