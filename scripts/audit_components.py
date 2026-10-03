"""Read fixed public upstream files; retain hashes, never vendor their code."""
import concurrent.futures
import hashlib
import json
from pathlib import Path
from urllib.request import Request, urlopen


COMPONENTS = [
    ("Sushi771/wewe-rss-ss", "4cd64f1a50cfd222b2c065e456528b200aac9370", "MIT", {
        "LICENSE": ["MIT License"],
        "apps/server/src/configuration.ts": ["weread.111965.xyz"],
        "apps/server/src/trpc/trpc.service.ts": ["refreshAllMpArticlesAndUpdateFeed", "getHistoryMpArticles", "inProgressHistoryMp"],
    }),
    ("rachelos/we-mp-rss", "126993c81a00466e9a6bbab041eef34ab27abe9c", "MIT", {
        "LICENSE": ["MIT License"],
        "core/wx/model/free_publish.py": ["begin", "count"],
        "docs/weread-mp.md": ["最新"],
    }),
    ("xpzouying/xiaohongshu-mcp", "a5c8f7799980ba1fdd501999843eb2d17e4c9a9f", "Apache-2.0", {
        "LICENSE": ["Apache License"],
        "xiaohongshu/user_profile.go": ["__INITIAL_STATE__", "notes"],
        "xiaohongshu/types.go": ["Video", "UserID"],
    }),
    ("JoeanAmier/XHS-Downloader", "3261312721f0b37c705ba6515885bc7f34349f2f", "GPL-3.0 (README additional statements need review)", {
        "LICENSE": ["GNU GENERAL PUBLIC LICENSE"],
        "source/application/user_posted.py": ["def run", "..."],
        "README.md": ["商业"],
    }),
]


def inspect(component):
    repo, commit, license_label, paths = component
    files = []
    for path, needles in paths.items():
        url = f"https://raw.githubusercontent.com/{repo}/{commit}/{path}"
        request = Request(url, headers={"User-Agent": "CreatorArchive-StaticAudit/0.1"})
        with urlopen(request, timeout=30) as response:
            data = response.read()
        lines = data.decode("utf-8-sig").splitlines()
        matches = {needle: [i for i, line in enumerate(lines, 1) if needle in line] for needle in needles}
        files.append({"path": path, "source": url, "sha256": hashlib.sha256(data).hexdigest(),
                      "bytes": len(data), "marker_lines": matches})
    return {"repository": repo, "commit": commit, "license_label": license_label,
            "evidence_level": "static_source_only", "adopted": False, "files": files}


if __name__ == "__main__":
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(inspect, COMPONENTS))
    output = Path("docs/validation/component-audit.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"schema_version": 1, "checked_on": "2026-09-26",
                                  "components": results}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Recorded {len(results)} fixed revisions / {sum(len(r['files']) for r in results)} files: {output}")
    missing = [(r["repository"], f["path"], n) for r in results for f in r["files"]
               for n, lines in f["marker_lines"].items() if not lines]
    print(f"Unmatched review markers: {missing}")
