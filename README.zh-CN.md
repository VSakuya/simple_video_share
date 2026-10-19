# Simple Video Share

[English](./README.md) | [日本語](./README.ja.md) | [简体中文](./README.zh-CN.md)


这是一个为**低性能服务器**设计的轻量级视频分享 Web 应用。所有高负载处理
（转码、剪辑、封面提取）都在**浏览器客户端**完成（通过 WebCodecs + mediabunny，
必要时回退到 ffmpeg.wasm）。服务器只负责临时缓存文件（LRU 淘汰，保留 500 MB
可用空间），并通过后台 worker 同步到 **Google Drive**。

## 功能

- 多用户账号（由管理员创建，不支持自助注册），支持头像，并强制首次登录修改密码。
- 上传队列支持客户端转码（AV1/H.264，限制为 1080p / 60 fps / 管理员设定码率），
  可选 QuickSplit 风格剪辑器，并支持 1 GB 以上文件分片上传。
- 观看页面支持 HTTP Range 流式播放、按需从 Google Drive 下载并缓存、评论
  （两级回复，支持颜文字）、标签和下载按钮。
- 支持文件夹（树形嵌套，深度不限）和标签系统。
- 直播房间（Apache 后的 Node-Media-Server FLV 流、开播检测、封面/标题管理、
  在线状态 + 持久化聊天）。
- 一起看房间（同步播放、在线状态、临时聊天）。
- 管理后台：视频 / 用户 / 直播房间 / 标签 / 设置 / 服务端与客户端日志，以及带
  校验和验证的存储导入/导出。

## 依赖与要求

- Python **3.12** 及以上（建议在 `.venv/` 虚拟环境中运行）。
- `pip install -r requirements.txt`（flask、pydrive2、waitress）。
- 在项目根目录准备 `config.json`（可复制 `config.example.json`）；secret key
  会在首次启动时自动生成。
- `credentials/` 目录下放置 Google Drive OAuth 凭据（运行时由应用管理，请勿手动
  修改）。
- 生产环境使用仓库内的 Apache vhost `000-default.conf`
  （HTTPS、反向代理到 `127.0.0.1:8080` 的 Waitress、FLV 播放保护）。

## 运行方式

- 本地开发: `python main.py`
