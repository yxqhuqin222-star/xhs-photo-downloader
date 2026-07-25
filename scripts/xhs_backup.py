#!/usr/bin/env python3
"""Local Xiaohongshu Live Photo backup tool.

The browser path can export note links from a logged-in profile. Media file
downloading is delegated to XHS-Downloader when configured.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

SUPPORTED_EXTENSIONS = {".heic", ".mov", ".jpg", ".jpeg", ".png", ".mp4", ".zip"}
LIVE_PHOTO_PAIRS = ((".heic", ".mov"), (".jpg", ".mp4"))
MANIFEST_FIELDS = [
    "顺序",
    "发布时间",
    "笔记标题",
    "笔记链接",
    "原始文件",
    "最终文件",
    "文件类型",
    "状态",
    "下载时间",
]
FAILED_FIELDS = ["笔记ID", "链接", "发布时间", "失败原因", "时间"]
NOTE_LINK_FIELDS = ["笔记ID", "链接", "导出时间"]
DATE_TIME_PATTERN = re.compile(r"(20\d{2}[-/]\d{1,2}[-/]\d{1,2})\s+(\d{1,2}:\d{2}(?::\d{2})?)")
XHS_DATA_FAILED_PATTERN = re.compile(r"([0-9a-fA-F]{24})\s+获取数据失败")
NOTE_LINKS_EVALUATE_SCRIPT = """anchors => {
    const tokenById = new Map();
    const visit = root => {
        const visited = new WeakSet();
        const stack = [{ value: root, depth: 0 }];
        let scanned = 0;
        while (stack.length && scanned < 5000) {
            const { value, depth } = stack.pop();
            scanned += 1;
            if (!value || typeof value !== "object") continue;
            if (visited.has(value)) continue;
            visited.add(value);
            const noteId = value.id || value.noteId;
            if (noteId && value.xsecToken) {
                tokenById.set(String(noteId), String(value.xsecToken));
            }
            if (depth >= 10) continue;
            if (Array.isArray(value)) {
                for (const item of value) {
                    stack.push({ value: item, depth: depth + 1 });
                }
                continue;
            }
            for (const item of Object.values(value)) {
                stack.push({ value: item, depth: depth + 1 });
            }
        }
    };
    const state = window.__INITIAL_STATE__;
    visit(state?.user?.notes);
    visit(state?.user?.userPageData);
    if (!tokenById.size) visit(state);
    return anchors.map(anchor => {
        const url = new URL(anchor.href);
        const noteId = url.pathname.split("/").filter(Boolean).at(-1);
        const token = noteId ? tokenById.get(noteId) : "";
        if (token && !url.searchParams.has("xsec_token")) {
            url.searchParams.set("xsec_token", token);
            url.searchParams.set("xsec_source", "pc_user");
        }
        return url.href;
    }).filter(Boolean);
}"""


@dataclass(frozen=True)
class AppConfig:
    profile_url: str
    sample_note_url: str
    max_notes: int
    headless: bool
    download_path: Path
    retry: int
    delay_range: tuple[float, float]
    download_wait_seconds: int
    manual_on_fail: bool
    manual_prompt_limit: int
    save_button_texts: tuple[str, ...]
    xhs_downloader_path: Path
    xhs_downloader_python: str
    xhs_downloader_work_path: Path
    xhs_downloader_folder_name: str
    xhs_downloader_cookie: str
    xhs_downloader_image_format: str
    xhs_downloader_live_download: bool
    xhs_downloader_download_record: bool
    xhs_downloader_batch_size: int

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        raw = json.loads(path.read_text(encoding="utf-8"))
        delay = raw.get("delay_range", [2, 5])
        if len(delay) != 2 or delay[0] > delay[1]:
            raise ValueError("config.delay_range must be [min, max]")
        return cls(
            profile_url=str(raw.get("profile_url", "")).strip(),
            sample_note_url=str(raw.get("sample_note_url", "")).strip(),
            max_notes=int(raw.get("max_notes", 0)),
            headless=bool(raw.get("headless", False)),
            download_path=Path(raw.get("download_path", "./output/xhs-live-photo-backup")),
            retry=max(1, int(raw.get("retry", 3))),
            delay_range=(float(delay[0]), float(delay[1])),
            download_wait_seconds=max(5, int(raw.get("download_wait_seconds", 30))),
            manual_on_fail=bool(raw.get("manual_on_fail", True)),
            manual_prompt_limit=max(0, int(raw.get("manual_prompt_limit", 5))),
            save_button_texts=tuple(raw.get("save_button_texts", ["保存", "下载"])),
            xhs_downloader_path=Path(raw.get("xhs_downloader_path", "./tools/XHS-Downloader")),
            xhs_downloader_python=str(raw.get("xhs_downloader_python", "python3")),
            xhs_downloader_work_path=Path(raw.get("xhs_downloader_work_path", "./output/xhs-downloader")),
            xhs_downloader_folder_name=str(raw.get("xhs_downloader_folder_name", "Download")),
            xhs_downloader_cookie=str(raw.get("xhs_downloader_cookie", "")),
            xhs_downloader_image_format=str(raw.get("xhs_downloader_image_format", "HEIC")),
            xhs_downloader_live_download=bool(raw.get("xhs_downloader_live_download", True)),
            xhs_downloader_download_record=bool(raw.get("xhs_downloader_download_record", False)),
            xhs_downloader_batch_size=max(1, int(raw.get("xhs_downloader_batch_size", 20))),
        )


class OutputLayout:
    def __init__(self, base: Path) -> None:
        self.base = base
        self.raw = base / "raw-downloads"
        self.sorted = base / "sorted"
        self.logs = base / "logs"
        self.diagnostics = self.logs / "diagnostics"
        self.credentials = base / "state"
        self.xhs_cookie = self.credentials / "xhs_cookie.txt"
        self.state = base / "state.json"
        self.manifest = self.logs / "manifest.csv"
        self.failed = self.logs / "failed.csv"
        self.note_links = self.logs / "note_links.csv"

    def ensure(self) -> None:
        for directory in (self.raw, self.sorted, self.logs, self.diagnostics, self.credentials):
            directory.mkdir(parents=True, exist_ok=True)
        if not self.state.exists():
            self.state.write_text(json.dumps({"completed_notes": []}, ensure_ascii=False, indent=2), encoding="utf-8")
        ensure_csv(self.manifest, MANIFEST_FIELDS)
        ensure_csv(self.failed, FAILED_FIELDS)
        ensure_csv(self.note_links, NOTE_LINK_FIELDS)


class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data = self._load()

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"completed_notes": []}
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        completed = raw.get("completed_notes", [])
        if not isinstance(completed, list):
            raise ValueError("state.completed_notes must be a list")
        return {"completed_notes": completed}

    def is_completed(self, note_id: str) -> bool:
        return note_id in self.data["completed_notes"]

    def mark_completed(self, note_id: str) -> None:
        if note_id not in self.data["completed_notes"]:
            self.data["completed_notes"].append(note_id)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

    def reset_note(self, note_id: str) -> None:
        if note_id in self.data["completed_notes"]:
            self.data["completed_notes"].remove(note_id)
            self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")


def ensure_csv(path: Path, fields: list[str]) -> None:
    if path.exists():
        return
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        csv.DictWriter(handle, fieldnames=fields).writeheader()


def append_csv(path: Path, fields: list[str], row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writerow({field: row.get(field, "") for field in fields})


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_published_at(value: str) -> datetime:
    clean = value.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(clean, fmt)
        except ValueError:
            pass
    raise ValueError(f"发布时间无法解析: {value!r}; 请使用 YYYY-MM-DD HH:MM[:SS]")


def extract_published_at_from_text(text: str) -> str:
    match = DATE_TIME_PATTERN.search(text)
    if not match:
        raise ValueError("页面中没有找到明确发布时间，需人工提供 YYYY-MM-DD HH:MM[:SS]")
    date_part = match.group(1).replace("/", "-")
    time_part = match.group(2)
    if len(time_part.split(":")) == 2:
        time_part = f"{time_part}:00"
    parsed = parse_published_at(f"{date_part} {time_part}")
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def published_at_from_note_id(note_id: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{24}", note_id):
        raise ValueError(f"无法从笔记 ID 推导发布时间: {note_id}")
    parsed = datetime.fromtimestamp(int(note_id[:8], 16))
    return parsed.strftime("%Y-%m-%d %H:%M:%S")


def safe_note_id(url: str) -> str:
    match = re.search(r"/(?:explore|discovery/item)/([A-Za-z0-9]+)", url)
    if match:
        return match.group(1)
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return f"url_{digest[:12]}"


def normalize_note_href(href: str) -> str:
    return href.split("#", 1)[0]


def note_link_has_xsec_token(url: str) -> bool:
    return bool(parse_qs(urlparse(url).query).get("xsec_token"))


def save_xhs_cookie(path: Path, cookies: list[dict[str, Any]]) -> bool:
    values: list[str] = []
    for cookie in cookies:
        domain = str(cookie.get("domain", "")).lstrip(".").lower()
        if (
            cookie.get("name")
            and cookie.get("value")
            and (domain == "xiaohongshu.com" or domain.endswith(".xiaohongshu.com"))
        ):
            values.append(f"{cookie['name']}={cookie['value']}")
    if not values:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write("; ".join(values))
    path.chmod(0o600)
    return True


def read_xhs_cookie(path: Path) -> str:
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def count_links_missing_xsec_token(note_links: list[str]) -> int:
    return sum(not note_link_has_xsec_token(url) for url in note_links)


def split_note_links_by_token(note_links: list[str]) -> tuple[list[str], list[str]]:
    downloadable: list[str] = []
    skipped: list[str] = []
    for note_url in note_links:
        target = downloadable if note_link_has_xsec_token(note_url) else skipped
        target.append(note_url)
    return downloadable, skipped


def command_for_display(command: list[str]) -> str:
    redacted = [
        re.sub(r"([?&]xsec_token=)[^&\s]+", r"\1<已隐藏>", value)
        for value in command
    ]
    if "-ck" in redacted:
        cookie_index = redacted.index("-ck") + 1
        if cookie_index < len(redacted):
            redacted[cookie_index] = "<已隐藏>"
    return " ".join(redacted)


def note_url_by_id(note_links: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for note_url in note_links:
        mapping[safe_note_id(note_url)] = note_url
    return mapping


def extract_failed_note_urls(output: str, note_links: list[str]) -> list[str]:
    mapping = note_url_by_id(note_links)
    failed_urls: list[str] = []
    seen: set[str] = set()
    for note_id in XHS_DATA_FAILED_PATTERN.findall(output):
        note_url = mapping.get(note_id)
        if note_url and note_url not in seen:
            failed_urls.append(note_url)
            seen.add(note_url)
    return failed_urls


def run_xhs_downloader_command(command: list[str], cwd: Path) -> tuple[int, str]:
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output_parts: list[str] = []
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="")
        output_parts.append(line)
    return process.wait(), "".join(output_parts)


def retry_failed_xhs_notes(
    config: AppConfig,
    layout: OutputLayout,
    note_links: list[str],
    cookie: str,
    dry_run: bool,
) -> list[str]:
    retry_links = list(dict.fromkeys(note_links))
    if not retry_links:
        return []

    print(f"准备单篇重试 {len(retry_links)} 个获取数据失败的作品。")
    unresolved = retry_links
    max_attempts = max(1, config.retry)
    for attempt in range(1, max_attempts + 1):
        current = unresolved
        unresolved = []
        print(f"开始第 {attempt}/{max_attempts} 轮单篇重试，作品数: {len(current)}")
        for note_url in current:
            note_id = safe_note_id(note_url)
            command = build_xhs_downloader_command(config, [note_url], cookie=cookie)
            if dry_run:
                print(command_for_display(command))
                continue
            returncode, output = run_xhs_downloader_command(command, config.xhs_downloader_path)
            failed_again = extract_failed_note_urls(output, [note_url])
            if returncode == 0 and not failed_again:
                print(f"作品 {note_id} 重试成功")
                continue
            unresolved.append(note_url)
            print(f"作品 {note_id} 第 {attempt} 轮重试仍失败，退出码: {returncode}", file=sys.stderr)
            time.sleep(random.uniform(*config.delay_range))
        if dry_run or not unresolved:
            break

    for note_url in unresolved:
        append_failure(layout, safe_note_id(note_url), note_url, "", f"XHS-Downloader 单篇重试 {max_attempts} 次后仍失败")
    return unresolved


def add_xsec_token_to_note_url(url: str, xsec_token: str, xsec_source: str = "pc_user") -> str:
    if not xsec_token or note_link_has_xsec_token(url):
        return url
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["xsec_token"] = [xsec_token]
    query.setdefault("xsec_source", [xsec_source])
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def prefer_note_link(current: str, candidate: str) -> str:
    if not current:
        return candidate
    if note_link_has_xsec_token(candidate) and not note_link_has_xsec_token(current):
        return candidate
    return current


def normalize_ext(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".jpeg":
        return ".jpg"
    return ext


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 10000):
        candidate = path.with_name(f"{stem}_{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"cannot find unique filename for {path}")


def copy_raw_file(source: Path, raw_dir: Path) -> Path:
    if source.parent.resolve() == raw_dir.resolve():
        return source
    destination = unique_path(raw_dir / source.name)
    if source.resolve() == destination.resolve():
        return source
    shutil.copy2(source, destination)
    return destination


def build_groups(raw_files: Iterable[Path]) -> list[list[Path]]:
    by_stem: dict[str, list[Path]] = {}
    singles: list[Path] = []
    for file_path in raw_files:
        ext = normalize_ext(file_path)
        if ext not in SUPPORTED_EXTENSIONS:
            continue
        stem = file_path.stem.lower()
        by_stem.setdefault(stem, []).append(file_path)

    groups: list[list[Path]] = []
    for files in by_stem.values():
        exts = {normalize_ext(path) for path in files}
        if any(still_ext in exts and motion_ext in exts for still_ext, motion_ext in LIVE_PHOTO_PAIRS):
            groups.append(sorted(files, key=lambda path: normalize_ext(path)))
        else:
            singles.extend(sorted(files))

    groups.extend([[file_path] for file_path in singles])
    return groups


def files_created_after(directory: Path, start: datetime) -> list[Path]:
    if not directory.exists():
        return []
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file()
        and normalize_ext(path) in SUPPORTED_EXTENSIONS
        and datetime.fromtimestamp(path.stat().st_mtime) >= start
    )


def organize_note_downloads(
    layout: OutputLayout,
    state: StateStore,
    note_id: str,
    note_url: str,
    published_at: str,
    title: str,
    downloaded_files: list[Path],
) -> list[Path]:
    if state.is_completed(note_id):
        return []

    published_dt = parse_published_at(published_at)
    copied_raw = [copy_raw_file(path, layout.raw) for path in downloaded_files if path.exists()]
    if not copied_raw:
        raise RuntimeError("没有可整理的下载文件")

    existing_hashes = {
        sha256_file(path)
        for path in layout.sorted.iterdir()
        if path.is_file() and normalize_ext(path) in SUPPORTED_EXTENSIONS
    }

    final_files: list[Path] = []
    sequence = 1
    for group in build_groups(copied_raw):
        prefix = f"{published_dt:%Y-%m-%d_%H%M%S}_{sequence:02d}"
        group_outputs: list[Path] = []
        for raw_path in group:
            ext = normalize_ext(raw_path)
            if ext not in SUPPORTED_EXTENSIONS:
                continue
            file_hash = sha256_file(raw_path)
            if file_hash in existing_hashes:
                append_manifest(layout, sequence, published_at, title, note_url, raw_path, "", ext, "skipped_duplicate")
                continue
            final_path = unique_path(layout.sorted / f"{prefix}{ext}")
            shutil.copy2(raw_path, final_path)
            existing_hashes.add(file_hash)
            final_files.append(final_path)
            group_outputs.append(final_path)
            append_manifest(layout, sequence, published_at, title, note_url, raw_path, final_path, ext, "saved")
        if group_outputs:
            sequence += 1

    state.mark_completed(note_id)
    return final_files


def supported_files_in(directory: Path) -> list[Path]:
    if not directory.exists():
        raise RuntimeError(f"目录不存在: {directory}")
    return sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and normalize_ext(path) in SUPPORTED_EXTENSIONS
    )


def append_manifest(
    layout: OutputLayout,
    sequence: int,
    published_at: str,
    title: str,
    note_url: str,
    raw_path: Path,
    final_path: Path | str,
    ext: str,
    status: str,
) -> None:
    append_csv(
        layout.manifest,
        MANIFEST_FIELDS,
        {
            "顺序": sequence,
            "发布时间": published_at,
            "笔记标题": title,
            "笔记链接": note_url,
            "原始文件": str(raw_path),
            "最终文件": str(final_path),
            "文件类型": ext,
            "状态": status,
            "下载时间": datetime.now().isoformat(timespec="seconds"),
        },
    )


def append_failure(layout: OutputLayout, note_id: str, note_url: str, published_at: str, reason: str) -> None:
    append_csv(
        layout.failed,
        FAILED_FIELDS,
        {
            "笔记ID": note_id,
            "链接": note_url,
            "发布时间": published_at,
            "失败原因": reason,
            "时间": datetime.now().isoformat(timespec="seconds"),
        },
    )


def append_note_links(layout: OutputLayout, note_links: list[str]) -> int:
    existing_ids: set[str] = set()
    existing_rows: list[dict[str, str]] = []
    row_by_note_id: dict[str, dict[str, str]] = {}
    if layout.note_links.exists():
        with layout.note_links.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                note_id = row.get("笔记ID", "")
                existing_rows.append(row)
                if note_id:
                    existing_ids.add(note_id)
                    row_by_note_id[note_id] = row

    exported = 0
    updated = False
    exported_at = datetime.now().isoformat(timespec="seconds")
    for note_url in note_links:
        note_id = safe_note_id(note_url)
        if note_id in existing_ids:
            row = row_by_note_id.get(note_id)
            if row:
                preferred = prefer_note_link(row.get("链接", ""), note_url)
                if preferred != row.get("链接", ""):
                    row["链接"] = preferred
                    row["导出时间"] = exported_at
                    updated = True
            continue
        row = {
            "笔记ID": note_id,
            "链接": note_url,
            "导出时间": exported_at,
        }
        append_csv(
            layout.note_links,
            NOTE_LINK_FIELDS,
            row,
        )
        existing_rows.append(row)
        row_by_note_id[note_id] = row
        existing_ids.add(note_id)
        exported += 1
    if updated:
        with layout.note_links.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=NOTE_LINK_FIELDS)
            writer.writeheader()
            for row in existing_rows:
                writer.writerow({field: row.get(field, "") for field in NOTE_LINK_FIELDS})
    return exported


def read_note_links(path: Path) -> list[str]:
    if not path.exists():
        raise RuntimeError(f"链接文件不存在: {path}")
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = csv.DictReader(handle)
            links: list[str] = []
            for row in rows:
                note_url = row.get("链接", "").strip() or row.get("笔记链接", "").strip()
                if note_url:
                    links.append(note_url)
            return links
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def bool_arg(value: bool) -> str:
    return "true" if value else "false"


def build_xhs_downloader_command(
    config: AppConfig,
    note_links: list[str],
    cookie: str = "",
) -> list[str]:
    main_py = (config.xhs_downloader_path / "main.py").resolve()
    if not main_py.exists():
        raise RuntimeError(
            f"未找到 XHS-Downloader: {main_py}; "
            "请先把 https://github.com/JoeanAmier/XHS-Downloader 克隆到 config.xhs_downloader_path"
        )
    command = [
        config.xhs_downloader_python,
        str(main_py),
        "-u",
        " ".join(note_links),
        "-wp",
        str(config.xhs_downloader_work_path.resolve()),
        "-fn",
        config.xhs_downloader_folder_name,
        "-if",
        config.xhs_downloader_image_format,
        "-ld",
        bool_arg(config.xhs_downloader_live_download),
        "-dr",
        bool_arg(config.xhs_downloader_download_record),
        "-aa",
        "true",
        "-wm",
        "true",
        "-l",
        "zh_CN",
    ]
    effective_cookie = config.xhs_downloader_cookie or cookie
    if effective_cookie:
        command.extend(["-ck", effective_cookie])
    return command


async def random_pause(config: AppConfig) -> None:
    await asyncio.sleep(random.uniform(*config.delay_range))


async def save_page_diagnostics(page: Any, layout: OutputLayout) -> None:
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    screenshot = layout.diagnostics / f"download_failed_{timestamp}.png"
    texts_file = layout.diagnostics / f"clickable_texts_{timestamp}.txt"
    html_file = layout.diagnostics / f"page_snapshot_{timestamp}.html"

    try:
        clickable_texts = await page.locator("button, [role=button], a").evaluate_all(
            """elements => elements
                .map((el, index) => ({
                    index,
                    tag: el.tagName,
                    role: el.getAttribute('role') || '',
                    aria: el.getAttribute('aria-label') || '',
                    title: el.getAttribute('title') || '',
                    text: (el.innerText || el.textContent || '').trim().replace(/\\s+/g, ' ')
                }))
                .filter(item => item.text || item.aria || item.title)
                .slice(0, 200)
                .map(item => `${item.index}\\t${item.tag}\\trole=${item.role}\\taria=${item.aria}\\ttitle=${item.title}\\ttext=${item.text}`)
            """
        )
        texts_file.write_text("\n".join(clickable_texts), encoding="utf-8")
        print(f"已保存可点击元素文本: {texts_file}")
    except Exception as exc:
        print(f"保存可点击元素文本失败，不影响主流程: {exc}", file=sys.stderr)

    try:
        html_file.write_text(await page.content(), encoding="utf-8")
        print(f"已保存页面 HTML 快照: {html_file}")
    except Exception as exc:
        print(f"保存页面 HTML 快照失败，不影响主流程: {exc}", file=sys.stderr)

    try:
        await page.screenshot(path=str(screenshot), full_page=False, timeout=5000, animations="disabled")
        print(f"已保存失败诊断截图: {screenshot}")
    except Exception as exc:
        print(f"保存失败诊断截图失败，不影响主流程: {exc}", file=sys.stderr)


def is_browser_closed_error(exc: Exception) -> bool:
    message = str(exc)
    return "Target page, context or browser has been closed" in message


async def collect_downloads(page: Any, layout: OutputLayout, action: Any, wait_seconds: int) -> list[Path]:
    pending_downloads = []

    def on_download(download: Any) -> None:
        pending_downloads.append(download)

    page.on("download", on_download)
    await action()
    await page.wait_for_timeout(wait_seconds * 1000)
    page.remove_listener("download", on_download)

    downloads: list[Path] = []
    for download in pending_downloads:
        destination = unique_path(layout.raw / download.suggested_filename)
        await download.save_as(destination)
        downloads.append(destination)
        print(f"已捕获浏览器真实下载: {destination}")
    return downloads


async def dismiss_known_popups(page: Any) -> None:
    for text in ("我知道了", "知道了", "稍后再说", "取消"):
        locator = page.get_by_text(text, exact=True).first
        try:
            if await locator.is_visible(timeout=700):
                await locator.click(timeout=1000)
                await page.wait_for_timeout(500)
        except Exception:
            continue


async def manual_download_fallback(config: AppConfig, layout: OutputLayout, page: Any, note_url: str) -> list[Path]:
    print(f"自动点击未捕获下载: {note_url}")
    print(f"请在 Chromium 中手动点击这篇笔记的真实保存/下载入口，程序将监听 {config.download_wait_seconds} 秒。")
    input("准备好后按回车，然后立刻到 Chromium 页面点击保存/下载...")
    return await collect_downloads(
        page,
        layout,
        lambda: asyncio.sleep(0),
        config.download_wait_seconds,
    )


async def capture_real_download(config: AppConfig, layout: OutputLayout, url: str) -> list[Path]:
    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeoutError
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright。请先运行: python3 -m pip install -r requirements.txt && python3 -m playwright install chromium") from exc

    downloads: list[Path] = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=config.headless)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        print("请在打开的 Chromium 中完成小红书登录；不要在终端输入密码。")
        await page.goto(url, wait_until="domcontentloaded")
        input("登录并确认已停留在目标笔记页面后，按回车开始点击保存按钮...")

        for text in config.save_button_texts:
            locator = page.get_by_text(text, exact=False).first
            try:
                await locator.wait_for(timeout=3000)
                print(f"尝试点击包含文本的控件: {text}")
                downloads.extend(
                    await collect_downloads(
                        page,
                        layout,
                        locator.click,
                        min(config.download_wait_seconds, 8),
                    )
                )
                await random_pause(config)
                if downloads:
                    break
            except PlaywrightTimeoutError:
                continue

        if not downloads:
            print(f"自动点击未捕获下载。请在 Chromium 中手动点击真实保存/下载按钮，程序将监听 {config.download_wait_seconds} 秒。")
            input("准备好手动点击后按回车，然后立刻到 Chromium 页面点击保存/下载...")
            downloads.extend(
                await collect_downloads(
                    page,
                    layout,
                    lambda: asyncio.sleep(0),
                    config.download_wait_seconds,
                )
            )

        if not downloads:
            await save_page_diagnostics(page, layout)

        await context.close()
        await browser.close()

    if not downloads:
        raise RuntimeError("未捕获到 Playwright download 事件；请确认页面存在真实保存按钮，或更新 config.save_button_texts")
    return downloads


async def fetch_note_downloads(config: AppConfig, layout: OutputLayout, page: Any, note_url: str, allow_manual: bool = False) -> list[Path]:
    await page.goto(note_url, wait_until="domcontentloaded")
    await random_pause(config)
    await dismiss_known_popups(page)
    downloads: list[Path] = []
    for text in config.save_button_texts:
        locator = page.get_by_text(text, exact=False).first
        try:
            await locator.wait_for(timeout=3000)
            downloads.extend(
                await collect_downloads(
                    page,
                    layout,
                    locator.click,
                    min(config.download_wait_seconds, 8),
                )
            )
            if downloads:
                break
        except Exception:
            continue
    if not downloads and allow_manual:
        downloads.extend(await manual_download_fallback(config, layout, page, note_url))
    if not downloads:
        await save_page_diagnostics(page, layout)
    return downloads


async def extract_note_metadata(page: Any) -> tuple[str, str]:
    title = await page.title()
    text = await page.locator("body").inner_text(timeout=5000)
    return title.strip(), extract_published_at_from_text(text)


async def extract_note_metadata_with_fallback(page: Any, note_id: str) -> tuple[str, str]:
    title = await page.title()
    text = await page.locator("body").inner_text(timeout=5000)
    try:
        return title.strip(), extract_published_at_from_text(text)
    except ValueError:
        published_at = published_at_from_note_id(note_id)
        print(f"页面没有明确发布时间，使用笔记 ID 推导发布时间: {published_at}")
        return title.strip(), published_at


async def collect_note_links(page: Any, max_notes: int) -> list[str]:
    seen: list[str] = []
    seen_note_ids: set[str] = set()
    max_scrolls = 20 if max_notes == 0 else max(5, min(40, max_notes * 2))
    for _ in range(max_scrolls):
        links = await page.locator("a[href*='/explore/'], a[href*='/discovery/item/']").evaluate_all(
            NOTE_LINKS_EVALUATE_SCRIPT
        )
        for href in links:
            normalized = normalize_note_href(href)
            note_id = safe_note_id(normalized)
            if note_id not in seen_note_ids:
                seen_note_ids.add(note_id)
                seen.append(normalized)
                if max_notes and len(seen) >= max_notes:
                    return seen
        await page.mouse.wheel(0, 1800)
        await page.wait_for_timeout(1200)
    return seen


async def run_backup_profile(config: AppConfig) -> int:
    if not config.profile_url:
        print("缺少个人主页 URL：请在 config/config.json 填写 profile_url。")
        return 2
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright。请先运行: python3 -m pip install -r requirements.txt && python3 -m playwright install chromium") from exc

    layout = OutputLayout(config.download_path)
    layout.ensure()
    state = StateStore(layout.state)
    completed = 0
    failed = 0
    manual_prompts_used = 0

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=config.headless)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        print("请在打开的 Chromium 中完成小红书登录；不要在终端输入密码。")
        await page.goto(config.profile_url, wait_until="domcontentloaded")
        input("请先在 Chromium 中登录并停留在你的个人主页；确认无误后回到终端按回车，脚本才会开始收集笔记链接...")
        note_links = await collect_note_links(page, config.max_notes)
        print(f"已收集 {len(note_links)} 个笔记链接。")

        for note_url in note_links:
            note_id = safe_note_id(note_url)
            if state.is_completed(note_id):
                print(f"跳过已完成笔记: {note_id}")
                continue
            try:
                started_at = datetime.now()
                allow_manual = config.manual_on_fail and manual_prompts_used < config.manual_prompt_limit
                downloads = await fetch_note_downloads(config, layout, page, note_url, allow_manual=allow_manual)
                if allow_manual and downloads:
                    manual_prompts_used += 1
                if not downloads:
                    raise RuntimeError("未捕获到浏览器下载事件")
                print(f"笔记 {note_id} 已下载 {len(downloads)} 个文件。")
                title, published_at = await extract_note_metadata_with_fallback(page, note_id)
                final_files = organize_note_downloads(layout, state, note_id, note_url, published_at, title, downloads or files_created_after(layout.raw, started_at))
                print(f"笔记 {note_id} 已整理 {len(final_files)} 个文件。")
                completed += 1
            except Exception as exc:
                if is_browser_closed_error(exc):
                    append_failure(layout, note_id, note_url, "", f"浏览器已关闭，停止批量任务: {exc}")
                    print("浏览器已关闭，停止批量任务。", file=sys.stderr)
                    break
                append_failure(layout, note_id, note_url, "", str(exc))
                print(f"笔记 {note_id} 下载失败: {exc}", file=sys.stderr)
                failed += 1
            await random_pause(config)

        await context.close()
        await browser.close()

    print(f"批量下载结束：成功 {completed}，失败 {failed}。原始文件保留在: {layout.raw}")
    return 0 if failed == 0 else 1


async def run_export_links(config: AppConfig) -> int:
    if not config.profile_url:
        print("缺少个人主页 URL：请在 config/config.json 填写 profile_url。")
        return 2
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError("缺少 Playwright。请先运行: python3 -m pip install -r requirements.txt && python3 -m playwright install chromium") from exc

    layout = OutputLayout(config.download_path)
    layout.ensure()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=config.headless)
        context = await browser.new_context(accept_downloads=True)
        page = await context.new_page()
        print("请在打开的 Chromium 中完成小红书登录；不要在终端输入密码。")
        await page.goto(config.profile_url, wait_until="domcontentloaded")
        input("请先在 Chromium 中登录并停留在你的个人主页；确认无误后回到终端按回车，脚本才会开始收集笔记链接...")
        note_links = await collect_note_links(page, config.max_notes)
        cookies = await context.cookies(["https://www.xiaohongshu.com"])
        cookie_saved = save_xhs_cookie(layout.xhs_cookie, cookies)
        await context.close()
        await browser.close()

    exported = append_note_links(layout, note_links)
    tokenized = len(note_links) - count_links_missing_xsec_token(note_links)
    print(f"已收集 {len(note_links)} 个笔记链接，其中 {tokenized} 个带访问令牌，新增导出 {exported} 个。")
    print(f"链接文件: {layout.note_links}")
    if cookie_saved:
        print(f"登录凭证已安全保存: {layout.xhs_cookie}")
    else:
        print("未获取到小红书登录凭证；请确认 Chromium 中已登录。", file=sys.stderr)
    return 0


def run_xhs_downloader(config: AppConfig, args: argparse.Namespace) -> int:
    layout = OutputLayout(config.download_path)
    layout.ensure()
    links_file = Path(args.links_file) if args.links_file else layout.note_links
    note_links = read_note_links(links_file)
    if args.limit:
        note_links = note_links[: args.limit]
    if not note_links:
        print(f"链接文件为空: {links_file}")
        return 2

    saved_cookie = read_xhs_cookie(layout.xhs_cookie)
    note_links, skipped_links = split_note_links_by_token(note_links)
    if skipped_links:
        skipped_ids = ", ".join(safe_note_id(url) for url in skipped_links)
        print(f"跳过 {len(skipped_links)} 个缺少 xsec_token 的旧链接: {skipped_ids}")
    if not note_links:
        print("没有带有效 xsec_token 的链接可供下载。", file=sys.stderr)
        return 2
    if saved_cookie and not config.xhs_downloader_cookie:
        print("已使用 export-links 保存的小红书登录凭证。")

    config.xhs_downloader_work_path.mkdir(parents=True, exist_ok=True)
    batches = list(chunked(note_links, config.xhs_downloader_batch_size))
    failed_batches = 0
    retry_note_urls: list[str] = []
    print(f"准备调用 XHS-Downloader 下载 {len(note_links)} 个笔记链接，共 {len(batches)} 批。")
    for index, batch in enumerate(batches, start=1):
        command = build_xhs_downloader_command(config, batch, cookie=saved_cookie)
        print(f"开始第 {index}/{len(batches)} 批，链接数: {len(batch)}")
        if args.dry_run:
            print(command_for_display(command))
            continue
        returncode, output = run_xhs_downloader_command(command, config.xhs_downloader_path)
        failed_note_urls = extract_failed_note_urls(output, batch)
        if failed_note_urls:
            retry_note_urls.extend(failed_note_urls)
            failed_ids = ", ".join(safe_note_id(url) for url in failed_note_urls)
            print(f"第 {index} 批检测到 {len(failed_note_urls)} 个单篇获取失败，稍后重试: {failed_ids}")
        elif returncode != 0:
            failed_batches += 1
            append_failure(layout, f"xhs_downloader_batch_{index}", " ".join(batch), "", f"XHS-Downloader 退出码 {returncode}")
            print(f"第 {index} 批下载失败，退出码: {returncode}", file=sys.stderr)

    unresolved = retry_failed_xhs_notes(config, layout, retry_note_urls, saved_cookie, args.dry_run)
    if failed_batches or unresolved:
        print(f"XHS-Downloader 批量下载结束：失败批次 {failed_batches} 个，仍失败作品 {len(unresolved)} 个。")
        return 1
    print(f"XHS-Downloader 批量下载结束。下载目录: {(config.xhs_downloader_work_path / config.xhs_downloader_folder_name).resolve()}")
    return 0


async def run_verify(config: AppConfig) -> int:
    layout = OutputLayout(config.download_path)
    layout.ensure()
    target_url = config.sample_note_url or config.profile_url
    if not target_url:
        print("缺少真实验证 URL：请在 config/config.json 填写 sample_note_url 或 profile_url。")
        print("当前已完成本地代码和测试入口；真实网页下载验证需要用户账号登录和样例笔记。")
        return 2
    try:
        await capture_real_download(config, layout, target_url)
    except Exception as exc:
        append_failure(layout, "manual_verify", target_url, "", str(exc))
        print(f"真实下载验证失败: {exc}", file=sys.stderr)
        return 1
    print(f"真实下载验证通过，下载文件已保留在: {layout.raw}")
    return 0


def run_organize_sample(config: AppConfig, args: argparse.Namespace) -> int:
    layout = OutputLayout(config.download_path)
    layout.ensure()
    state = StateStore(layout.state)
    note_id = args.note_id or safe_note_id(args.note_url)
    files = [Path(path) for path in args.files]
    try:
        final_files = organize_note_downloads(layout, state, note_id, args.note_url, args.published_at, args.title, files)
    except Exception as exc:
        append_failure(layout, note_id, args.note_url, args.published_at, str(exc))
        print(f"整理失败: {exc}", file=sys.stderr)
        return 1
    print(f"已生成 {len(final_files)} 个 sorted 文件")
    for path in final_files:
        print(path)
    return 0


def run_organize_raw(config: AppConfig, args: argparse.Namespace) -> int:
    layout = OutputLayout(config.download_path)
    layout.ensure()
    state = StateStore(layout.state)
    note_url = args.note_url or config.sample_note_url or config.profile_url or "manual_raw"
    note_id = args.note_id or safe_note_id(note_url)
    if args.force:
        state.reset_note(note_id)
    files = supported_files_in(layout.raw)
    try:
        final_files = organize_note_downloads(layout, state, note_id, note_url, args.published_at, args.title, files)
    except Exception as exc:
        append_failure(layout, note_id, note_url, args.published_at, str(exc))
        print(f"整理 raw-downloads 失败: {exc}", file=sys.stderr)
        return 1
    print(f"已从 raw-downloads 生成 {len(final_files)} 个 sorted 文件")
    for path in final_files:
        print(path)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="小红书 Live Photo 自动备份整理工具")
    parser.add_argument("--config", default="config/config.json", help="配置文件路径")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("verify-download", help="打开 Chromium 并验证真实保存按钮下载事件")
    subparsers.add_parser("backup-profile", help="从个人主页收集笔记链接并批量触发真实浏览器下载")
    subparsers.add_parser("export-links", help="从个人主页收集笔记链接并导出 CSV，不下载文件")
    xhs = subparsers.add_parser("download-with-xhs", help="调用外部 XHS-Downloader 下载导出的笔记链接")
    xhs.add_argument("--links-file", default="", help="链接 CSV 或纯文本文件；默认使用 logs/note_links.csv")
    xhs.add_argument("--limit", type=int, default=0, help="只下载前 N 个链接，用于小样本验证")
    xhs.add_argument("--dry-run", action="store_true", help="只打印将执行的 XHS-Downloader 命令")

    organize = subparsers.add_parser("organize-files", help="整理一组已下载文件，用于本地验证和恢复流程")
    organize.add_argument("--note-url", required=True)
    organize.add_argument("--note-id", default="")
    organize.add_argument("--published-at", required=True, help="YYYY-MM-DD HH:MM[:SS]")
    organize.add_argument("--title", default="")
    organize.add_argument("files", nargs="+")

    raw = subparsers.add_parser("organize-raw", help="整理 raw-downloads 中现有文件")
    raw.add_argument("--note-url", default="")
    raw.add_argument("--note-id", default="")
    raw.add_argument("--published-at", required=True, help="YYYY-MM-DD HH:MM[:SS]")
    raw.add_argument("--title", default="")
    raw.add_argument("--force", action="store_true", help="重新整理已完成 note_id")

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    config = AppConfig.load(Path(args.config))

    if args.command in (None, "verify-download"):
        return asyncio.run(run_verify(config))
    if args.command == "backup-profile":
        return asyncio.run(run_backup_profile(config))
    if args.command == "export-links":
        return asyncio.run(run_export_links(config))
    if args.command == "download-with-xhs":
        return run_xhs_downloader(config, args)
    if args.command == "organize-files":
        return run_organize_sample(config, args)
    if args.command == "organize-raw":
        return run_organize_raw(config, args)
    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
