"""Bounded page/content experiment with independent durable checkpoints.

Only stable identities and page cursors are persisted. Browser access references
are refreshed by replaying the original list page after process restart.
"""
from contextlib import closing
from pathlib import Path
import sqlite3
import time
from urllib.parse import quote
from .validation import AdapterFailure


def initialize(workflow):
    with closing(sqlite3.connect(workflow.db_path)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='page_pipelines'").fetchone():
            return None
        backup = None
        if db.execute("SELECT 1 FROM jobs UNION ALL SELECT 1 FROM items UNION ALL SELECT 1 FROM subscriptions UNION ALL SELECT 1 FROM subscription_intents LIMIT 1").fetchone():
            directory = workflow.db_path.parent / "backups"
            directory.mkdir(exist_ok=True)
            backup = directory / f"archive-before-page-pipeline-{time.time_ns()}.sqlite3"
            with closing(sqlite3.connect(backup)) as target:
                db.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("数据库备份校验失败，未执行按页实验升级")
        db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE page_pipelines (
                parent_job_id INTEGER PRIMARY KEY REFERENCES jobs(id),
                page_limit INTEGER NOT NULL CHECK(page_limit=2),
                stage TEXT NOT NULL DEFAULT 'list');
            CREATE TABLE pipeline_pages (
                parent_job_id INTEGER NOT NULL REFERENCES page_pipelines(parent_job_id),
                page_number INTEGER NOT NULL,
                child_job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id),
                PRIMARY KEY(parent_job_id,page_number));
            COMMIT;
        """)
        return str(backup) if backup else None


def progress(db, parent_id):
    # Duplicate pinned items appear in several pages; report one content result.
    return [dict(row) for row in db.execute("""
        SELECT j.item_id, CASE WHEN max(j.state='succeeded') THEN 'succeeded'
            WHEN max(j.state='partial') THEN 'partial' ELSE 'queued' END AS state,
            max(CASE WHEN j.state='partial' THEN j.reason END) AS reason
        FROM pipeline_pages p JOIN job_items j ON j.job_id=p.child_job_id
        WHERE p.parent_job_id=? GROUP BY j.platform,j.item_id ORDER BY j.item_id
    """, (parent_id,))]


def _reuse_complete(service, child):
    with service.workflow.connect() as db:
        targets = [dict(row) for row in db.execute("""
            SELECT i.* FROM job_items j JOIN items i USING(platform,item_id)
            WHERE j.job_id=? AND j.state!='succeeded'
            AND i.detail_state='complete' AND i.media_state='complete_for_observed_detail'
        """, (child["id"],))]
    for item in targets:
        with service.workflow.connect() as db:
            assets = [row[0] for row in db.execute("SELECT asset_id FROM assets WHERE platform=? AND item_id=?", (item["platform"], item["item_id"]))]
        if assets and all(service.workflow.asset_valid(item["platform"], item["item_id"], asset) for asset in assets):
            with service.workflow.connect() as db:
                db.execute("UPDATE job_items SET state='succeeded',reason=NULL WHERE job_id=? AND item_id=?", (child["id"], item["item_id"]))


def _stage(service, parent_id, stage):
    with service.workflow.connect() as db:
        db.execute("UPDATE page_pipelines SET stage=? WHERE parent_job_id=?", (stage, parent_id))


def execute(service, job):
    workflow = service.workflow
    parent_id = job["id"]
    with workflow.connect() as db:
        if not job["run_id"]:
            batch = db.execute("INSERT INTO batches(mode,created_at) VALUES('page_archive',?)", (time.time(),)).lastrowid
            run_id = db.execute("INSERT INTO runs(batch_id,platform,author_id,mode,adapter_version,updated_at) VALUES(?,?,?,'page_archive','pending',?)", (batch,job["platform"],job["author_id"],time.time())).lastrowid
            db.execute("UPDATE jobs SET run_id=? WHERE id=?", (run_id,parent_id))
        else:
            run_id = job["run_id"]
        checkpoint = dict(db.execute("SELECT * FROM page_pipelines WHERE parent_job_id=?", (parent_id,)).fetchone())
    with service._network_lock:
        transport = service.transport()
        with workflow.connect() as db:
            run = dict(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            if run["adapter_version"] not in {"pending",transport.version}:
                service._finish(parent_id,"partial","adapter_version_changed")
                return
            db.execute("UPDATE runs SET adapter_version=? WHERE id=?", (transport.version,run_id))
        # Retry unfinished children first. Never replace original list evidence.
        with workflow.connect() as db:
            unfinished = [dict(row) for row in db.execute("""SELECT j.*,p.request_cursor FROM pipeline_pages q
                JOIN jobs j ON j.id=q.child_job_id JOIN pages p ON p.run_id=? AND p.page_number=q.page_number
                WHERE q.parent_job_id=? AND j.state!='succeeded' ORDER BY q.page_number""", (run_id,parent_id))]
        for child in unfinished:
            if service._stopping.is_set():
                service._finish(parent_id,"interrupted","process_interrupted")
                return
            _reuse_complete(service, child)
            with workflow.connect() as db:
                pending = db.execute("SELECT count(*) FROM job_items WHERE job_id=? AND state!='succeeded'", (child["id"],)).fetchone()[0]
            if pending:
                _stage(service,parent_id,"content")
                service._check_cooldown(job["platform"])
                transport.page(job["author_id"],child["request_cursor"] or None)
            if not _consume(service,parent_id,child):
                return
        while True:
            if service._stopping.is_set():
                service._finish(parent_id,"interrupted","process_interrupted")
                return
            with workflow.connect() as db:
                run = dict(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            if run["terminal_evidence"] or run["pages"] >= checkpoint["page_limit"]:
                break
            _stage(service,parent_id,"list")
            service._check_cooldown(job["platform"])
            page = transport.page(job["author_id"],run["cursor"])
            with workflow.connect() as db:
                db.execute("BEGIN IMMEDIATE")
                workflow._commit_page_in_db(db,run_id,run["cursor"],page)
                child_id = db.execute("INSERT INTO jobs(platform,author_id,mode,state,created_at,updated_at) VALUES(?,?,'content','queued',?,?)", (job["platform"],job["author_id"],time.time(),time.time())).lastrowid
                db.executemany("INSERT OR IGNORE INTO job_items(job_id,platform,item_id) VALUES(?,?,?)", [(child_id,job["platform"],item.item_id) for item in page.items])
                db.execute("INSERT INTO pipeline_pages VALUES(?,?,?)", (parent_id,run["pages"]+1,child_id))
                db.execute("UPDATE page_pipelines SET stage='content' WHERE parent_job_id=?", (parent_id,))
                child = dict(db.execute("SELECT * FROM jobs WHERE id=?", (child_id,)).fetchone())
            _reuse_complete(service,child)
            if not _consume(service,parent_id,child):
                return
        _stage(service,parent_id,"archive")
        with workflow.connect() as db:
            if not run["terminal_evidence"]:
                db.execute("UPDATE runs SET state='partial',coverage='partial',reason='page_budget_reached',updated_at=? WHERE id=?", (time.time(),run_id))
            pending = any(item["state"] != "succeeded" for item in progress(db,parent_id))
        exported = workflow.export_all(batch_id=run["batch_id"])
        for author in exported["authors"]:
            for key in ("manifest","corpus","index"):
                author[key + "_url"] = "/archive/" + quote(Path(author[key]).relative_to(service.root / "archive").as_posix(),safe="/")
        _stage(service,parent_id,"content" if pending else "done")
        service._finish(parent_id,"partial" if pending else "succeeded","page_archive_partial" if pending else "page_archive_complete",exported)


def _consume(service, parent_id, child):
    with service.workflow.connect() as db:
        pending = db.execute("SELECT count(*) FROM job_items WHERE job_id=? AND state!='succeeded'", (child["id"],)).fetchone()[0]
        db.execute("UPDATE jobs SET state='running',reason=NULL,updated_at=? WHERE id=?", (time.time(),child["id"]))
    if pending:
        try:
            service._execute_content(child,page_scoped=True)
        except Exception as error:
            category = error.category if isinstance(error,AdapterFailure) else "unexpected_error"
            service._finish(child["id"],category if category in {"needs_login","rate_limited"} else "blocked",category)
            raise
    else:
        service._finish(child["id"],"succeeded","content_complete")
    with service.workflow.connect() as db:
        result = dict(db.execute("SELECT * FROM jobs WHERE id=?", (child["id"],)).fetchone())
    if result["state"] not in {"succeeded","partial"}:
        service._finish(parent_id,result["state"],result["reason"])
        return False
    if service._stopping.is_set():
        service._finish(parent_id,"interrupted","process_interrupted")
        return False
    return True
