"""Run the unified workflow against synthetic pages only."""

import argparse
import json
from pathlib import Path

from creator_archive.validation import SyntheticAdapter
from creator_archive.workflow import ArchiveWorkflow


def main():
    parser = argparse.ArgumentParser(description="Creator Archive 双平台模拟工作流（不访问平台）")
    parser.add_argument("--root", type=Path, default=Path("data/validation/workflow-demo"))
    parser.add_argument("--batch-id", type=int, help="继续已有批次")
    parser.add_argument("--max-pages", type=int, default=100, help="本次每位作者最多请求页数")
    parser.add_argument("--mode", choices=("full", "latest", "archive"), default="archive")
    args = parser.parse_args()
    if args.max_pages < 1:
        parser.error("--max-pages 必须至少为 1")
    workflow = ArchiveWorkflow(args.root)
    if args.batch_id is None:
        for platform in ("wechat", "xiaohongshu"):
            for author_id in ("demo-a", "demo-b-paused"):
                workflow.subscribe(platform, author_id, author_id, verified=True,
                                   evidence="synthetic_demo")
        workflow.set_enabled("wechat", "demo-b-paused", False)
        workflow.set_enabled("xiaohongshu", "demo-b-paused", False)
    result = workflow.run_all({p: SyntheticAdapter() for p in ("wechat", "xiaohongshu")},
                              mode=args.mode, batch_id=args.batch_id, max_pages=args.max_pages)
    print(json.dumps({"evidence_level": "synthetic", "batch_id": result["batch_id"],
                      "runs": [{key: r[key] for key in ("platform", "author_id", "pages", "item_count",
                                                        "state", "coverage", "reason", "cursor")}
                               for r in result["runs"]],
                      "archive_root": result.get("export", {}).get("archive_root")},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
