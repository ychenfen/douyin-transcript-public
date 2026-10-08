# Short Video Transcript Public

独立部署的抖音、微信视频号链接转文字与素材下载服务。抖音链接默认免密码；微信视频号链接使用单独的访问密码和元宝 Web 登录态。转写完成后可下载 TXT、原视频和投研工作台素材包。

这个仓库与旧 `dy2text` 项目完全分离：使用独立 Git 历史、服务器目录、systemd 服务、端口和 Nginx 限流区。旧服务可保留为回滚候选。

## 功能

- 抖音分享口令或短链解析
- 微信视频号 `weixin.qq.com/sph/...` 分享链接直接解析和转写
- 本地视频/音频文件转写（视频号、录屏等没有抖音链接的素材）
- 匿名 Chromium、iesdouyin、yt-dlp 多后端降级下载
- SenseVoice / FunASR 中文语音转写与作品级缓存
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

测试全部离线运行，覆盖受保护模式、抖音免密码、微信单独鉴权、视频/ZIP 下载、页面 CSP、缓存、微信解析错误和下载后端降级。真实视频号端到端验证需要有效的元宝登录态。
