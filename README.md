# 小红书 Live Photo 自动备份整理工具

macOS 本地工具。它使用 Playwright 打开 Chromium，依赖用户手动登录自己的小红书账号，先导出账号主页里的笔记链接，再调用外部 [XHS-Downloader](https://github.com/JoeanAmier/XHS-Downloader) 自动解析并下载作品文件。

## 核心边界

- 本项目不直接请求图片接口，媒体解析与下载交给外部 XHS-Downloader。
- 本项目不复制 XHS-Downloader 源码，只按外部工具调用。
- 本项目不抓取 `img src`。
- 不保存用户密码。
- 不绕过登录、验证码、权限或平台限制。
- 不做格式转换，原样保留 `.heic`、`.mov`、`.jpg`、`.mp4`、`.png`、`.zip`。

## 目录结构

```text
.
├── config/config.example.json
├── scripts/xhs_backup.py
├── tests/test_xhs_backup.py
├── run.command
├── requirements.txt
└── output/xhs-live-photo-backup/
    ├── raw-downloads/
    ├── sorted/
    ├── logs/
    │   ├── manifest.csv
    │   ├── failed.csv
    │   ├── note_links.csv
    │   └── diagnostics/
    ├── state/
    │   └── xhs_cookie.txt
    └── state.json
```

`config/config.json` 和 `output/` 是本地私有文件，已写入 `.gitignore`，后续上 GitHub 时不要提交。

## 安装

```bash
cd /Users/kityhello/workplace/geren/xhs_photo
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
git clone https://github.com/JoeanAmier/XHS-Downloader.git tools/XHS-Downloader
cd tools/XHS-Downloader
python3 -m pip install -r requirements.txt
```

## 配置

先创建本地配置：

```bash
cp config/config.example.json config/config.json
```

然后编辑 `config/config.json`：

```json
{
  "profile_url": "",
  "sample_note_url": "https://www.xiaohongshu.com/explore/...",
  "max_notes": 0,
  "headless": false,
  "download_path": "./output/xhs-live-photo-backup",
  "retry": 3,
  "delay_range": [2, 5],
  "download_wait_seconds": 30,
  "manual_on_fail": false,
  "manual_prompt_limit": 5,
  "save_button_texts": ["保存", "下载"],
  "xhs_downloader_path": "./tools/XHS-Downloader",
  "xhs_downloader_python": "python3",
  "xhs_downloader_work_path": "./output/xhs-downloader",
  "xhs_downloader_folder_name": "Download",
  "xhs_downloader_cookie": "",
  "xhs_downloader_image_format": "HEIC",
  "xhs_downloader_live_download": true,
  "xhs_downloader_download_record": false,
  "xhs_downloader_batch_size": 20
}
```

- `sample_note_url`：单篇样例笔记 URL，用于验证真实保存按钮是否会触发下载。
- `profile_url`：个人主页 URL，用于后续批量收集笔记链接。
- `max_notes`：批量模式最多处理多少篇；`0` 表示不主动限制。
- `manual_on_fail`：旧浏览器保存模式的人工兜底开关；新流程建议保持 `false`。
- `xhs_downloader_path`：XHS-Downloader 项目路径。
- `xhs_downloader_work_path`：XHS-Downloader 的下载根目录。
- `xhs_downloader_cookie`：小红书网页版 Cookie，可选。默认由 `export-links` 登录后自动保存，无需手工填写。
- `xhs_downloader_live_download`：是否让 XHS-Downloader 下载 Live Photo 动态文件。
- `xhs_downloader_download_record`：是否使用 XHS-Downloader 自带下载记录；默认关闭，避免更换输出目录后误跳过。
- `xhs_downloader_batch_size`：每批传给 XHS-Downloader 的笔记链接数量。

## 推荐流程

先导出账号主页笔记链接：

```bash
./run.command export-links
```

流程：

1. Chromium 打开 `profile_url`。
2. 在 Chromium 中手动登录，不要在终端输入密码。
3. 确认页面停留在目标账号主页后，回到终端按回车。
4. 程序滚动主页并导出链接到 `output/xhs-live-photo-backup/logs/note_links.csv`。
5. 登录 Cookie 保存到 `output/xhs-live-photo-backup/state/xhs_cookie.txt`，文件权限为 `600`；不要分享或提交该文件。

先用 2 条链接验证 XHS-Downloader：

```bash
./run.command download-with-xhs --limit 2
```

下载命令会自动读取上一步保存的 Cookie。Cookie 不能替代访问令牌；发现缺少 `xsec_token` 的旧链接时会显示笔记 ID、跳过该项并继续下载其余链接。若 XHS-Downloader 输出单篇“获取数据失败”，程序会提取失败作品 ID，按单篇重试 `config.retry` 次，最终仍失败才写入 `logs/failed.csv`。

确认下载正常后，下载全部导出的链接：

```bash
./run.command download-with-xhs
```

如果想先检查将要执行的外部命令：

```bash
./run.command download-with-xhs --limit 2 --dry-run
```

## 旧浏览器保存验证

```bash
./run.command verify-download
```

流程：

1. Chromium 打开小红书页面。
2. 在 Chromium 中手动登录，不要在终端输入密码。
3. 确认页面停留在目标笔记后，回到终端按回车。
4. 如果自动点击没有捕获下载，且 `manual_on_fail` 为 `true`，程序会要求你手动点击网页上的真实保存/下载入口。
5. 成功后，原始文件会保留在 `output/xhs-live-photo-backup/raw-downloads/`。

如果失败，诊断文件会写到：

```text
output/xhs-live-photo-backup/logs/diagnostics/
```

## 整理已下载文件

真实下载通过后，可以把 `raw-downloads/` 里的文件整理到 `sorted/`：

```bash
./run.command organize-raw \
  --note-id sample-note-001 \
  --note-url "https://www.xiaohongshu.com/explore/..." \
  --published-at "2026-07-24 15:30:21" \
  --title "样例 Live Photo"
```

命名规则：

```text
YYYY-MM-DD_HHMMSS_XX.ext
```

支持 Live Photo 配对：

- `.heic + .mov`
- `.jpg + .mp4`

同一组 Live Photo 的静态图和动态视频会使用完全相同的文件名前缀。

## 旧批量模式

```bash
./run.command backup-profile
```

旧批量模式会打开 `profile_url`，收集个人主页中的笔记链接，然后逐篇尝试通过网页真实保存按钮触发下载。新流程不建议使用它，优先使用 `export-links` + `download-with-xhs`。

第一次建议先小批量：

```json
{
  "profile_url": "https://www.xiaohongshu.com/user/profile/...",
  "max_notes": 3
}
```

确认 `logs/note_links.csv` 和 XHS-Downloader 下载目录都正常后，再把 `max_notes` 改大；`0` 表示尽量收集全部。

当前第一版限制：

- 优先使用页面中明确的 `YYYY-MM-DD HH:MM[:SS]` 或 `YYYY/MM/DD HH:MM[:SS]` 发布时间。
- 如果页面只显示“昨天”“3天前”“07-24”这类相对时间，会用小红书 24 位 note_id 的前 8 位推导发布时间，并在终端提示这是兜底值；不会用下载时间冒充发布时间。
- 不绕过平台弹窗、登录、验证码或权限限制。
- 下载失败会写入 `logs/failed.csv`。
- 新流程下载结果由 XHS-Downloader 保存到 `xhs_downloader_work_path`。
- 小红书链接里的 `xsec_token` 可能过期，建议导出后尽快下载。
- Cookie 只作为补充登录凭证，不能让裸链接正常访问。缺少 `xsec_token` 的旧链接会被保留在 CSV 中并在下载时跳过；令牌或 Cookie 过期后重新运行 `export-links` 刷新。

## 测试

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

测试覆盖：

- `.heic + .mov` 使用相同前缀。
- `.jpg + .mp4` 使用相同前缀。
- 已完成笔记会被 `state.json` 跳过。
- 无法解析的发布时间不会被静默替换为下载时间。
- 导出的笔记链接会按 note_id 去重。
- XHS-Downloader 桥接命令会使用外部工具路径。

## GitHub 准备

上 GitHub 前建议检查：

```bash
find . -maxdepth 3 -type f | sort
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests
```

不要提交这些内容：

- `output/`
- `tools/XHS-Downloader/`
- `config/config.json`
- `__pycache__/`
- `.DS_Store`
- 任何账号、Cookie、浏览器个人数据或下载的私人照片/视频。
