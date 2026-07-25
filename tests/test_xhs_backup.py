import json
from pathlib import Path
import unittest

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import xhs_backup
from xhs_backup import (
    OutputLayout,
    AppConfig,
    NOTE_LINKS_EVALUATE_SCRIPT,
    StateStore,
    add_xsec_token_to_note_url,
    append_note_links,
    build_xhs_downloader_command,
    command_for_display,
    count_links_missing_xsec_token,
    extract_published_at_from_text,
    extract_failed_note_urls,
    is_browser_closed_error,
    normalize_note_href,
    organize_note_downloads,
    parse_published_at,
    published_at_from_note_id,
    read_xhs_cookie,
    save_xhs_cookie,
    split_note_links_by_token,
    supported_files_in,
)


def write_file(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


class XhsBackupTests(unittest.TestCase):
    def test_config_loads_manual_fallback_defaults(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profile_url": "",
                        "sample_note_url": "",
                        "max_notes": 0,
                        "headless": False,
                        "download_path": "./output/xhs-live-photo-backup",
                        "retry": 3,
                        "delay_range": [2, 5],
                        "download_wait_seconds": 30,
                        "manual_on_fail": False,
                        "manual_prompt_limit": 5,
                        "save_button_texts": ["保存", "下载"],
                        "xhs_downloader_path": "./tools/XHS-Downloader",
                        "xhs_downloader_python": "python3",
                        "xhs_downloader_work_path": "./output/xhs-downloader",
                        "xhs_downloader_folder_name": "Download",
                        "xhs_downloader_cookie": "",
                        "xhs_downloader_image_format": "HEIC",
                        "xhs_downloader_live_download": True,
                        "xhs_downloader_download_record": False,
                        "xhs_downloader_batch_size": 20,
                    }
                ),
                encoding="utf-8",
            )
            config = AppConfig.load(config_path)
            self.assertFalse(config.manual_on_fail)
            self.assertEqual(config.manual_prompt_limit, 5)
            self.assertEqual(config.xhs_downloader_path, Path("tools/XHS-Downloader"))
            self.assertTrue(config.xhs_downloader_live_download)

    def test_append_note_links_deduplicates_by_note_id(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            layout = OutputLayout(Path(directory) / "output")
            layout.ensure()
            links = [
                "https://www.xiaohongshu.com/explore/63f9de4300000000270286fc?xsec_token=one",
                "https://www.xiaohongshu.com/explore/63f9de4300000000270286fc?xsec_token=two",
                "https://www.xiaohongshu.com/explore/658d8ccd0000000012000df8?xsec_token=three",
            ]

            first = append_note_links(layout, links)
            second = append_note_links(layout, links)

            rows = layout.note_links.read_text(encoding="utf-8-sig").splitlines()
            self.assertEqual(first, 2)
            self.assertEqual(second, 0)
            self.assertEqual(len(rows), 3)

    def test_append_note_links_updates_existing_bare_link_with_token(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            layout = OutputLayout(Path(directory) / "output")
            layout.ensure()
            bare = "https://www.xiaohongshu.com/explore/63f9de4300000000270286fc"
            tokenized = f"{bare}?xsec_token=one&xsec_source=pc_user"

            first = append_note_links(layout, [bare])
            second = append_note_links(layout, [tokenized])

            rows = layout.note_links.read_text(encoding="utf-8-sig").splitlines()
            self.assertEqual(first, 1)
            self.assertEqual(second, 0)
            self.assertEqual(len(rows), 2)
            self.assertIn(tokenized, rows[1])

    def test_add_xsec_token_to_note_url_keeps_existing_query(self):
        url = "https://www.xiaohongshu.com/explore/63f9de4300000000270286fc?foo=bar"

        updated = add_xsec_token_to_note_url(url, "token-value")

        self.assertIn("foo=bar", updated)
        self.assertIn("xsec_token=token-value", updated)
        self.assertIn("xsec_source=pc_user", updated)

    def test_save_xhs_cookie_filters_domain_and_restricts_permissions(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            cookie_path = Path(directory) / "state" / "xhs_cookie.txt"
            saved = save_xhs_cookie(
                cookie_path,
                [
                    {"name": "web_session", "value": "session-value", "domain": ".xiaohongshu.com"},
                    {"name": "a1", "value": "a1-value", "domain": "www.xiaohongshu.com"},
                    {"name": "foreign", "value": "ignore", "domain": "example.com"},
                    {"name": "lookalike", "value": "ignore", "domain": "evilxiaohongshu.com"},
                ],
            )

            self.assertTrue(saved)
            self.assertEqual(
                read_xhs_cookie(cookie_path),
                "web_session=session-value; a1=a1-value",
            )
            self.assertEqual(cookie_path.stat().st_mode & 0o777, 0o600)

    def test_bare_links_are_skipped_without_removing_tokenized_links(self):
        bare = "https://www.xiaohongshu.com/explore/63f9de4300000000270286fc"
        tokenized = f"{bare}?xsec_token=one&xsec_source=pc_user"

        self.assertEqual(count_links_missing_xsec_token([bare, tokenized]), 1)
        downloadable, skipped = split_note_links_by_token([bare, tokenized])
        self.assertEqual(downloadable, [tokenized])
        self.assertEqual(skipped, [bare])

    def test_note_link_script_prioritizes_user_notes(self):
        from playwright.sync_api import sync_playwright

        note_id = "63f9de4300000000270286fc"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_content(f'<a href="https://www.xiaohongshu.com/explore/{note_id}">note</a>')
            page.evaluate(
                """noteId => {
                    const noise = {};
                    let current = noise;
                    for (let index = 0; index < 6000; index += 1) {
                        current.next = {};
                        current = current.next;
                    }
                    window.__INITIAL_STATE__ = {
                        user: {
                            notes: {
                                _rawValue: [[{
                                    id: noteId,
                                    xsecToken: "token-value",
                                }]],
                            },
                        },
                        noise,
                    };
                }""",
                note_id,
            )

            links = page.locator("a").evaluate_all(NOTE_LINKS_EVALUATE_SCRIPT)
            browser.close()

        self.assertEqual(len(links), 1)
        self.assertIn("xsec_token=token-value", links[0])

    def test_command_display_redacts_cookie(self):
        displayed = command_for_display(
            [
                "python3",
                "main.py",
                "-u",
                "https://www.xiaohongshu.com/explore/note?xsec_token=token-value&xsec_source=pc_user",
                "-ck",
                "secret=value",
                "-l",
                "zh_CN",
            ]
        )

        self.assertNotIn("secret=value", displayed)
        self.assertNotIn("token-value", displayed)
        self.assertIn("xsec_token=<已隐藏>", displayed)
        self.assertIn("-ck <已隐藏>", displayed)

    def test_extract_failed_note_urls_maps_downloader_output_to_links(self):
        failed = "647e8d030000000013033f32 获取数据失败"
        links = [
            "https://www.xiaohongshu.com/explore/647e8d030000000013033f32?xsec_token=one",
            "https://www.xiaohongshu.com/explore/63f9de4300000000270286fc?xsec_token=two",
        ]

        self.assertEqual(extract_failed_note_urls(failed, links), [links[0]])

    def test_retry_failed_xhs_notes_retries_until_success(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            tool = tmp_path / "XHS-Downloader"
            tool.mkdir()
            (tool / "main.py").write_text("print('stub')\n", encoding="utf-8")
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profile_url": "",
                        "sample_note_url": "",
                        "max_notes": 0,
                        "headless": False,
                        "download_path": str(tmp_path / "backup"),
                        "retry": 2,
                        "delay_range": [0, 0],
                        "download_wait_seconds": 30,
                        "manual_on_fail": False,
                        "manual_prompt_limit": 5,
                        "save_button_texts": ["保存", "下载"],
                        "xhs_downloader_path": str(tool),
                        "xhs_downloader_python": "python3",
                        "xhs_downloader_work_path": str(tmp_path / "xhs-output"),
                        "xhs_downloader_folder_name": "Download",
                        "xhs_downloader_cookie": "",
                        "xhs_downloader_image_format": "HEIC",
                        "xhs_downloader_live_download": True,
                        "xhs_downloader_download_record": False,
                        "xhs_downloader_batch_size": 20,
                    }
                ),
                encoding="utf-8",
            )
            config = AppConfig.load(config_path)
            layout = OutputLayout(config.download_path)
            layout.ensure()
            note_url = "https://www.xiaohongshu.com/explore/647e8d030000000013033f32?xsec_token=one"
            calls = []

            def fake_run(command, cwd):
                calls.append(command)
                if len(calls) == 1:
                    return 2, "647e8d030000000013033f32 获取数据失败\n"
                return 0, "作品处理完成：647e8d030000000013033f32\n"

            original_run = xhs_backup.run_xhs_downloader_command
            try:
                xhs_backup.run_xhs_downloader_command = fake_run
                unresolved = xhs_backup.retry_failed_xhs_notes(config, layout, [note_url], "", False)
            finally:
                xhs_backup.run_xhs_downloader_command = original_run

            self.assertEqual(unresolved, [])
            self.assertEqual(len(calls), 2)
            self.assertNotIn(
                "647e8d030000000013033f32",
                layout.failed.read_text(encoding="utf-8-sig"),
            )

    def test_build_xhs_downloader_command_uses_external_tool(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            tool = tmp_path / "XHS-Downloader"
            tool.mkdir()
            (tool / "main.py").write_text("print('stub')\n", encoding="utf-8")
            config_path = tmp_path / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "profile_url": "",
                        "sample_note_url": "",
                        "max_notes": 0,
                        "headless": False,
                        "download_path": str(tmp_path / "backup"),
                        "retry": 3,
                        "delay_range": [2, 5],
                        "download_wait_seconds": 30,
                        "manual_on_fail": False,
                        "manual_prompt_limit": 5,
                        "save_button_texts": ["保存", "下载"],
                        "xhs_downloader_path": str(tool),
                        "xhs_downloader_python": "python3",
                        "xhs_downloader_work_path": str(tmp_path / "xhs-output"),
                        "xhs_downloader_folder_name": "Download",
                        "xhs_downloader_cookie": "cookie=value",
                        "xhs_downloader_image_format": "HEIC",
                        "xhs_downloader_live_download": True,
                        "xhs_downloader_download_record": False,
                        "xhs_downloader_batch_size": 20,
                    }
                ),
                encoding="utf-8",
            )
            config = AppConfig.load(config_path)

            command = build_xhs_downloader_command(
                config,
                ["https://www.xiaohongshu.com/explore/63f9de4300000000270286fc"],
            )

            self.assertEqual(command[:3], ["python3", str((tool / "main.py").resolve()), "-u"])
            self.assertIn("-ld", command)
            self.assertIn("true", command)
            self.assertEqual(command[command.index("-dr") + 1], "false")
            self.assertIn("-ck", command)
            self.assertIn("cookie=value", command)

            config_without_cookie = AppConfig(
                **{
                    **config.__dict__,
                    "xhs_downloader_cookie": "",
                }
            )
            command = build_xhs_downloader_command(
                config_without_cookie,
                ["https://www.xiaohongshu.com/explore/63f9de4300000000270286fc"],
                cookie="saved=value",
            )
            self.assertEqual(command[command.index("-ck") + 1], "saved=value")

    def test_live_photo_pair_uses_same_prefix(self):
        with self.subTest("same stem HEIC and MOV"):
            import tempfile

            with tempfile.TemporaryDirectory() as directory:
                tmp_path = Path(directory)
                layout = OutputLayout(tmp_path / "output")
                layout.ensure()
                state = StateStore(layout.state)
                source = tmp_path / "downloads"
                source.mkdir()
                heic = write_file(source / "asset_001.heic", b"heic-data")
                mov = write_file(source / "asset_001.mov", b"mov-data")

                final_files = organize_note_downloads(
                    layout,
                    state,
                    "note_001",
                    "https://www.xiaohongshu.com/explore/note_001",
                    "2026-07-24 15:30:21",
                    "Live Photo note",
                    [heic, mov],
                )

                names = sorted(path.name for path in final_files)
                self.assertEqual(names, ["2026-07-24_153021_01.heic", "2026-07-24_153021_01.mov"])
                prefixes = {Path(name).stem for name in names}
                self.assertEqual(prefixes, {"2026-07-24_153021_01"})

    def test_state_skips_completed_note(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            layout = OutputLayout(tmp_path / "output")
            layout.ensure()
            state = StateStore(layout.state)
            source = tmp_path / "downloads"
            source.mkdir()
            first = write_file(source / "one.png", b"same-note")

            first_run = organize_note_downloads(
                layout,
                state,
                "note_002",
                "https://www.xiaohongshu.com/explore/note_002",
                "2026-07-24 15:30:21",
                "",
                [first],
            )
            second_run = organize_note_downloads(
                layout,
                StateStore(layout.state),
                "note_002",
                "https://www.xiaohongshu.com/explore/note_002",
                "2026-07-24 15:30:21",
                "",
                [first],
            )

            self.assertEqual(len(first_run), 1)
            self.assertEqual(second_run, [])
            state_data = json.loads(layout.state.read_text(encoding="utf-8"))
            self.assertEqual(state_data["completed_notes"], ["note_002"])

    def test_web_live_photo_jpg_mp4_pair_uses_same_prefix(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            layout = OutputLayout(tmp_path / "output")
            layout.ensure()
            state = StateStore(layout.state)
            source = tmp_path / "downloads"
            source.mkdir()
            jpg = write_file(source / "记录75 喜欢这些照片.jpg", b"jpg-data")
            mp4 = write_file(source / "记录75 喜欢这些照片.mp4", b"mp4-data")

            final_files = organize_note_downloads(
                layout,
                state,
                "note_003",
                "https://www.xiaohongshu.com/explore/note_003",
                "2026-07-24 15:30:21",
                "Web Live Photo note",
                [jpg, mp4],
            )

            names = sorted(path.name for path in final_files)
            self.assertEqual(names, ["2026-07-24_153021_01.jpg", "2026-07-24_153021_01.mp4"])
            prefixes = {Path(name).stem for name in names}
            self.assertEqual(prefixes, {"2026-07-24_153021_01"})

    def test_supported_files_includes_mp4(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            mp4 = write_file(tmp_path / "asset.mp4", b"mp4-data")
            write_file(tmp_path / "notes.txt", b"not-media")
            self.assertEqual(supported_files_in(tmp_path), [mp4])

    def test_organizing_raw_directory_does_not_duplicate_raw_files(self):
        import tempfile

        with tempfile.TemporaryDirectory() as directory:
            tmp_path = Path(directory)
            layout = OutputLayout(tmp_path / "output")
            layout.ensure()
            state = StateStore(layout.state)
            jpg = write_file(layout.raw / "asset.jpg", b"jpg-data")
            mp4 = write_file(layout.raw / "asset.mp4", b"mp4-data")

            final_files = organize_note_downloads(
                layout,
                state,
                "note_004",
                "https://www.xiaohongshu.com/explore/note_004",
                "2026-07-24 15:30:21",
                "Raw directory note",
                [jpg, mp4],
            )

            self.assertEqual(sorted(path.name for path in final_files), ["2026-07-24_153021_01.jpg", "2026-07-24_153021_01.mp4"])
            self.assertEqual(sorted(path.name for path in layout.raw.iterdir()), ["asset.jpg", "asset.mp4"])

    def test_unparseable_published_at_is_not_silently_replaced(self):
        with self.assertRaises(ValueError):
            parse_published_at("昨天")

    def test_extract_published_at_from_page_text(self):
        self.assertEqual(
            extract_published_at_from_text("记录75 喜欢这些照片\n发布于 2026-07-24 15:30"),
            "2026-07-24 15:30:00",
        )

    def test_extract_published_at_rejects_relative_time(self):
        with self.assertRaises(ValueError):
            extract_published_at_from_text("昨天 15:30 发布")

    def test_browser_closed_error_is_detected(self):
        self.assertTrue(is_browser_closed_error(Exception("Page.goto: Target page, context or browser has been closed")))
        self.assertFalse(is_browser_closed_error(Exception("未捕获到浏览器下载事件")))

    def test_note_href_keeps_xsec_token(self):
        href = "https://www.xiaohongshu.com/explore/63f9de4300000000270286fc?xsec_token=abc&xsec_source=pc_user#comment"
        self.assertEqual(
            normalize_note_href(href),
            "https://www.xiaohongshu.com/explore/63f9de4300000000270286fc?xsec_token=abc&xsec_source=pc_user",
        )

    def test_published_at_from_note_id(self):
        self.assertEqual(published_at_from_note_id("63f9de4300000000270286fc"), "2023-02-25 18:09:07")


if __name__ == "__main__":
    unittest.main()
