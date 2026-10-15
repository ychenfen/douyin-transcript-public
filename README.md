# Short Video Transcript Public

抖音、B站、微信视频号链接转文字与素材下载服务。抖音和B站链接默认免密码；微信视频号链接使用单独的访问密码和元宝 Web 登录态。B站提取音频并导出 TXT、带时间戳的 Markdown 和 SRT；抖音和微信继续支持保留原视频及投研工作台素材包。

这个仓库与旧 `dy2text` 项目完全分离：使用独立 Git 历史、服务器目录、systemd 服务、端口和 Nginx 限流区。旧服务可保留为回滚候选。

## 功能

- 抖音分享口令或短链解析
- B站 BV / b23.tv 分享链接，按 `?p=` 选择分集、分别缓存
- 微信视频号 `weixin.qq.com/sph/...` 分享链接直接解析和转写
- 本地视频/音频文件转写（视频号、录屏等没有抖音链接的素材）
- 匿名 Chromium、iesdouyin、yt-dlp 多后端降级下载
- SenseVoice / FunASR 中文语音转写与作品级缓存
- 轻量 SenseVoice 每次读取约 30 秒音频；B站长音频支持分段进度和中断续跑
- 手机网页校对、复制、TXT 下载和最近处理记录
- 原视频下载
- 带清单和 SHA-256 的投研工作台 ZIP
- 原视频 24 小时保留和 2 GiB 总量上限
- nonce CSP、防 iframe、no-referrer、64 KiB 请求体上限
- Nginx 按 IP 请求限流和并发限制
- 可通过一个环境变量恢复口令保护

## 本地开发

需要 Python 3.10 及以上。

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

启动免密码开发服务：

```bash
DOUYIN_REQUIRE_AUTH=0 .venv/bin/python -m douyin_workflow serve --host 127.0.0.1 --port 8766
```

恢复口令保护：

```bash
DOUYIN_REQUIRE_AUTH=1 DOUYIN_SERVER_TOKEN='<从钥匙串或环境注入>' \
  .venv/bin/python -m douyin_workflow serve --host 127.0.0.1 --port 8766
```

浏览器口令仅写入 `sessionStorage`，关闭标签页后失效。生产可保持抖音免密码，同时只对微信视频号链接及其视频/素材包下载要求口令。

## B站分集与长视频转写

网页输入框、原有 `run` 命令和 HTTP `/transcribe` 都接受 B站链接；MCP 新增 `bilibili_to_text`。例如选择本合集第 16 P（2024 年计网真题）：

```bash
DOUYIN_ASR_MODEL=sherpa-sensevoice .venv/bin/python -m douyin_workflow run \
  'https://www.bilibili.com/video/BV1VE411A7Ak?p=16'
```

轻量后端需要 `pip install -e '.[lite]'`，并将已有模型目录通过 `DOUYIN_SHERPA_MODEL_DIR` 指定，其中包含 `model.int8.onnx`、`tokens.txt`。ffmpeg 优先使用 `DOUYIN_FFMPEG` 或系统命令，也可使用 lite 依赖附带的 imageio-ffmpeg。

- 支持公开 BV 视频和 `b23.tv` 短链；省略 `p` 时选第一 P，非法或不存在的 P 会报错，不会静默换集。
- 素材 ID 为 `bili-<BV号>-p<分P>`；请求第 1 P 和第 16 P 不会共用结果。
- 使用公开元数据和音轨接口，无需读取浏览器 Cookie。只下载最低码率音轨，不下载完整视频。单条音轨下载上限 512 MiB；登录、付费内容和仅提供试看音轨的内容不支持。
- 数据目录保存 `transcript.txt`、`transcript.md`、`transcript.srt`、`segments.json` 及元数据。网页可下载 TXT、Markdown、SRT。B站结果不显示原视频和工作台视频包按钮。
- Markdown/SRT 是原始分段识别稿，不包含网页文本框中的手动修改；TXT 会包含修改。分段通常为 20–30 秒，不是逐句对齐字幕。
- `sherpa-sensevoice` 每次只读取约 30 秒 PCM，长视频不整段载入内存。`segments.jsonl` 保存检查点，重试相同链接会在校验音频 SHA-256 和模型后续跑；`--force` 从头处理。其他 ASR 后端保持现有行为，时间戳只覆盖整段音频。
- 网页会显示已转写音频秒数，单次页面轮询最长 60 分钟；关闭页面不会取消服务端任务，重新提交同一链接可查看进度或缓存。
- 声音转写不会提取画面题干、拓扑图、硬字幕或公式，也不自动套用课程专用术语替换。数字、缩写及专有名词仍需校对。

长音频处理受服务器 CPU、排队和磁盘容量影响。音频、检查点和逐字稿遵循原有长期缓存行为，不属于视频 24 小时清理范围；部署前应为这些文件预留磁盘空间。

小内存服务器可设置 `DOUYIN_RELEASE_ASR_AFTER_JOB=1`：任务结束后清除模型缓存，Linux 下同时归还可释放的堆内存。下次任务需重新加载模型，轻量后端通常增加几秒。部署示例同时使用 `MALLOC_ARENA_MAX=2` 限制 glibc 内存池数量；这些环境配置需重启服务生效。未设置释放开关时保持原有模型常驻行为。

## 视频号与本地视频转写

### 分享链接直接转写

视频号的公开分享页只提供标题、作者和封面，不提供视频流。本项目使用已登录的腾讯元宝 Web 会话取得短期播放凭证，再下载、抽音频并转写：

```bash
WECHAT_YUANBAO_COOKIE='<从钥匙串或受限环境文件注入>' \
  .venv/bin/python -m douyin_workflow run 'https://weixin.qq.com/sph/...'
```

- 登录 Cookie 只从 `WECHAT_YUANBAO_COOKIE` 注入，不写入代码、日志、素材元数据或接口响应。
- 元宝 Cookie 通常约一个月失效；失效后重新登录并更新配置。服务器环境文件必须为 `0600`。
- 公网页面即使设置 `DOUYIN_REQUIRE_AUTH=0`，微信链接仍由 `WECHAT_REQUIRE_AUTH=1` 单独保护，访问密码复用 `DOUYIN_SERVER_TOKEN`。
- 这是个人工具，不应公开提供视频号解析能力；频繁请求可能触发元宝或视频号限制。

### 本地文件转写（备用）

元宝登录态失效、内容不可播放或只有本地录屏时，可以先取得本地文件再转写：

1. 下载：用开源工具 [wx_channels_download](https://github.com/ltaoo/wx_channels_download)（Windows / macOS）。装好后电脑版微信的视频号播放器会多一个下载按钮。它会安装本地证书并修改系统代理，首次运行需要管理员权限，请只从官方 Releases 获取。录屏得到的视频也可以直接用。
2. 转写：

```bash
.venv/bin/python -m douyin_workflow file ~/Downloads/某视频号.mp4 --title "冲刺阶段建议"
```

- 支持 ffmpeg 能读的任何视频或音频格式，原文件只读不动。
- 按文件内容 SHA-256 缓存，结果写入 `<数据目录>/file-<哈希前16位>/`，和抖音作品一起出现在 `list` 里。
- `--force` 重新转写，`--json` 输出完整元数据。
- MCP 工具 `video_file_to_text(path, title, force)` 做同样的事，可以在 Claude 里直接给本机路径。

网页端暂不支持上传文件：公网入口有 64 KiB 请求体上限，开放上传需要单独评估容量和滥用风险。

## 独立生产部署

默认生产标识：

| 项目 | 值 |
|---|---|
| 服务器目录 | `/home/ubuntu/dy2text-public` |
| 应用服务 | `dy2text-public.service` |
| Cookie 定时器 | `dy2text-public-cookie-refresh.timer` |
| 后端地址 | `127.0.0.1:8766` |
| 公网入口 | `https://video.yuchenxu.cn/dy/` |

部署文件位于 `deploy/linux/`：

- `dy2text.env.example`：独立路径和免密码开关
- `dy2text-public.service`：应用服务
- `dy2text-public-cookie-refresh.service` / `.timer`：匿名 Cookie 刷新
- `nginx-rate-limit.conf`：安装到 `/etc/nginx/conf.d/` 的全局限流区
- `nginx-location.conf`：站点内的 `/dy/` 反代

上线顺序：先建立独立目录和运行环境，再安装 systemd 单元；最后执行 `nginx -t`，通过后才切换 `/dy/` 反代。不要覆盖或删除旧 `/home/ubuntu/dy2text` 和 `dy2text.service`。

生产环境必须保留随机 `DOUYIN_SERVER_TOKEN`：`DOUYIN_REQUIRE_AUTH=0` 时抖音免密码，但 `WECHAT_REQUIRE_AUTH=1` 仍用它保护微信链接。需要临时收紧全站访问时，把 `DOUYIN_REQUIRE_AUTH` 改为 `1` 并重启应用即可，不需要重新部署代码。

## 安全与容量边界

免密码不等于无限开放。公网入口必须保留：

- 每 IP 30 次/分钟、突发 15 次的请求限制
- 每 IP 最多 3 个并发连接
- 应用全局单任务转写队列
- 64 KiB 请求体上限
- 视频 24 小时过期、总量 2 GiB

限流值考虑了网页每 2 秒一次的正常轮询。出现 HTTP 429 时，页面会提示稍后再试。

抖音下载不会读取或上传个人浏览器登录配置；它的 Cookie 刷新只创建匿名临时浏览器会话。微信直链功能只读取显式注入的元宝 Cookie，不读取浏览器配置，也不把 Cookie 写进输出。第三方页面可能继续变化，因此“长期可维护”依靠降级链、缓存、健康检查和可回滚部署，不代表接口永久不变。

下载素材不自动取得发布权。工作台素材包始终标记 `rights.status=unverified`，发布前仍需核验版权、肖像权、事实和平台规则。

## 测试

```bash
.venv/bin/pytest
```

测试全部离线运行，覆盖受保护模式、抖音/B站免密码、微信单独鉴权、视频/ZIP 下载、页面 CSP、缓存、微信解析错误和下载后端降级。B站测试另覆盖分 P/CID 选择、短链域名校验、试看拒绝、签名地址脱敏、时间戳导出、长音频有界读取、中断续跑和输入变化失效。完整长音频测试需安装 lite 依赖中的 numpy。真实视频号端到端验证需要有效的元宝登录态。
