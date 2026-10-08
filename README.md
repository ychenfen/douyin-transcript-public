# Douyin Transcript Public

独立部署的抖音链接转文字与素材下载服务。网页默认免密码，粘贴分享口令即可获得逐字稿，并可下载 TXT、原视频和投研工作台素材包。

这个仓库与旧 `dy2text` 项目完全分离：使用独立 Git 历史、服务器目录、systemd 服务、端口和 Nginx 限流区。旧服务可保留为回滚候选。

## 功能

- 抖音分享口令或短链解析
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

浏览器口令仅写入 `sessionStorage`，关闭标签页后失效；免密码模式不读取、不保存也不发送 Authorization。

## 视频号与本地视频转写

视频号没有公开下载接口，服务器拿不到视频，所以分两步：先在自己电脑上把视频下载成 mp4，再交给本项目转写。

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

生产环境建议保留一个随机 `DOUYIN_SERVER_TOKEN`，即使 `DOUYIN_REQUIRE_AUTH=0` 时不会使用它。需要临时收紧访问时，把开关改为 `1` 并重启应用即可，不需要重新部署代码。

## 安全与容量边界

免密码不等于无限开放。公网入口必须保留：

- 每 IP 30 次/分钟、突发 15 次的请求限制
- 每 IP 最多 3 个并发连接
- 应用全局单任务转写队列
- 64 KiB 请求体上限
- 视频 24 小时过期、总量 2 GiB

限流值考虑了网页每 2 秒一次的正常轮询。出现 HTTP 429 时，页面会提示稍后再试。

项目不会读取或上传个人浏览器登录配置；Cookie 刷新只创建匿名临时浏览器会话。第三方页面可能继续变化，因此“长期可维护”依靠降级链、缓存、健康检查和可回滚部署，不代表能保证抖音接口永久不变。

下载素材不自动取得发布权。工作台素材包始终标记 `rights.status=unverified`，发布前仍需核验版权、肖像权、事实和平台规则。

## 测试

```bash
.venv/bin/pytest
```

测试全部离线运行，覆盖受保护模式、免密码转写、免密码视频/ZIP 下载、页面 CSP、缓存和下载后端降级。
