"""Private, offline validation of Edge-exported XHS pagination responses."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile

from creator_archive.adapters.xhs_har import CapturedXhsAdapter, read_har
from creator_archive.workflow import ArchiveWorkflow
from creator_archive.validation import AdapterFailure


FORMAT = "creator-archive-xhs-har-v1"
MESSAGES = {
    "no_matching_user_posted_responses": "没有两位样本作者的分页响应；请先打开网络面板，再刷新作者主页并滚动后重新导出。",
    "har_too_large": "HAR超过128MiB；请缩短本次观察范围并另存一份，不覆盖旧记录。",
    "invalid_har": "文件不是可读取的HAR；请使用Edge的导出HAR（已清理）功能。",
    "invalid_capture_id_or_cursor": "作者ID或游标格式与当前适配合同不同；需检查实际响应后调整，不能当作末页。",
    "ambiguous_capture_request": "分页请求缺少唯一作者或游标，不能建立可信分页链；请重新导出包含请求URL的HAR。",
    "capture_time_missing": "缺少带时区的请求时间；请重新从Edge导出HAR。",
    "unsafe_root": "结果目录必须在本机LOCALAPPDATA内，不能使用仓库或同步目录。",
    "unrecognized_existing_root": "结果目录已有不匹配资料；请另选LOCALAPPDATA内的新目录，旧资料未覆盖。",
    "invalid_samples": "缺少既有XHS-A/B身份样本；请恢复原samples.json，勿在聊天中发送登录凭据。",
    "conflicting_captured_pages": "同一作者和游标出现不同成功响应；请保留两份记录供核查，不混合两次变化后的历史扫描。",
    "page_budget_must_be_positive": "每次处理页数必须大于0。",
}

NEXT_STEPS = {
    "missing_captured_page": "分页链在下一游标缺少响应；在同一作者页正常继续滚动并另存HAR，再导入同一结果目录。",
    "captured_needs_login": "该捕获页记录登录失败；请在现有Edge自行重新登录后，补充失败页响应。",
    "captured_rate_limited": "该捕获页记录限流；按平台提示等待冷却，再补充响应，旧记录不能证明当前已解除限流。",
    "captured_unavailable": "该捕获页缺正文或响应失败，原因需核查；补充含响应体的HAR，成功页已保留。",
    "page_budget_reached": "达到本次导入页数预算；再次运行同一目录继续，预算不是末页。",
}


def atomic_json(path, value):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".har-", suffix=".tmp", delete=False) as stream:
        temp = Path(stream.name)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def verify(hars, samples, root, max_pages=100):
    """The caller supplies a private root and known author identities, never cookies."""
    if max_pages < 1:
        raise ValueError("page_budget_must_be_positive")
    if not isinstance(samples, dict) or any(not isinstance(samples.get(label), dict) for label in ("XHS-A", "XHS-B")):
        raise ValueError("invalid_samples")
    aliases = {label: samples[label].get("author_id") for label in ("XHS-A", "XHS-B")}
    if any(not isinstance(a, str) or not re.fullmatch(r"[0-9a-f]{24}", a) for a in aliases.values()) or len(set(aliases.values())) != 2:
        raise ValueError("invalid_samples")
    root = Path(root)
    marker = root / "capture-index.json"
    if root.exists() and any(root.iterdir()) and not marker.exists():
        raise ValueError("unrecognized_existing_root")
    index = json.loads(marker.read_text(encoding="utf-8")) if marker.exists() else {
        "format": FORMAT, "authors": aliases, "records": [], "source_sha256": [], "batch_id": None}
    if not isinstance(index, dict) or index.get("format") != FORMAT or index.get("authors") != aliases:
        raise ValueError("unrecognized_existing_root")
    # Read/normalize all inputs before changing the private index or database.
    for har in hars:
        records, digest = read_har(Path(har), set(aliases.values()))
        if digest not in index["source_sha256"]:
            index["records"].extend(records)
            index["source_sha256"].append(digest)
    adapter = CapturedXhsAdapter(index["records"])
    # Conflicts also invalidate an already imported prefix: stop before any writes.
    for author, cursor in adapter.records:
        try:
            adapter.fetch(author, cursor)
        except AdapterFailure:
            if adapter.stop_reasons.get(author) == "conflicting_captured_pages":
                raise ValueError("conflicting_captured_pages") from None
    adapter.stop_reasons.clear()
    root.mkdir(parents=True, exist_ok=True)
    atomic_json(marker, index)
    workflow = ArchiveWorkflow(root)
    for label, author in aliases.items():
        workflow.subscribe("xiaohongshu", author, label, verified=True,
                           evidence="previous_verified_sample_identity_captured_response_only")
    batch_id = index["batch_id"]
    if batch_id is None:
        # Reuse a batch created immediately before an interrupted marker write.
        batch_id = workflow.status()["batch_id"]
    result = workflow.run_all({"xiaohongshu": adapter}, mode="full", batch_id=batch_id, max_pages=max_pages)
    index["batch_id"] = result["batch_id"]
    atomic_json(marker, index)
    labels = {a: label for label, a in aliases.items()}
    report = {"format": FORMAT, "evidence_level": "user_supplied_capture_replay",
              "live_transport_resume_verified": False, "g1_passed": False,
              "batch_id": result["batch_id"], "runs": []}
    for run in result["runs"]:
        reason = adapter.stop_reasons.get(run["author_id"], run["reason"])
        report["runs"].append({"sample": labels[run["author_id"]], "pages": run["pages"],
                               "unique_ids": run["item_count"], "state": run["state"],
                               "captured_chain_coverage": run["coverage"],
                               "reason": reason,
                               "next_step": ("已到捕获响应的明确末页；尚需单独验证真实采集进程恢复。" if run["state"] == "succeeded"
                                             else NEXT_STEPS.get(reason, "保留已有进度，核查失败游标及响应格式后重试。")),
                               "terminal_evidence": run["terminal_evidence"],
                               "next_cursor_sha256": sha256(run["cursor"].encode()).hexdigest() if run["cursor"] else None})
    atomic_json(root / "report.json", report)
    return report


def main():
    local = Path(os.environ.get("LOCALAPPDATA", Path.home() / ".local/share"))
    private = local / "CreatorArchive/private-validation/G1-2026-09-26"
    parser = argparse.ArgumentParser(description="验证现有Edge导出的XHS HAR分页链；不访问网络，不证明自动采集恢复。")
    parser.add_argument("--har", type=Path, nargs="+", required=True)
    parser.add_argument("--samples", type=Path, default=private / "samples.json")
    parser.add_argument("--root", type=Path, default=private / "xhs-har-validation")
    parser.add_argument("--max-pages", type=int, default=100)
    args = parser.parse_args()
    try:
        if not args.root.resolve().is_relative_to(local.resolve()):
            raise ValueError("unsafe_root")
        for env in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
            if os.environ.get(env) and args.root.resolve().is_relative_to(Path(os.environ[env]).resolve()):
                raise ValueError("unsafe_root")
        if args.root.resolve().is_relative_to(Path(__file__).resolve().parents[1]):
            raise ValueError("unsafe_root")
        samples = json.loads(args.samples.read_text(encoding="utf-8-sig"))
        report = verify(args.har, samples, args.root, args.max_pages)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print("结果目录：", args.root)
        print("这是捕获响应的离线导入恢复；真实认证采集进程恢复和双平台G1仍未通过。")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        message = MESSAGES.get(str(error), "记录缺失、冲突或格式不符；请检查本地HAR/样本文件，不覆盖旧资料后重试。")
        print("小红书HAR验证阶段未完成：" + message)
        print("已提交的分页检查点保留；补充对应响应后用同一结果目录继续。")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
