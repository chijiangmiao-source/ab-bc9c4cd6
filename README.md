# 深空地面站 32 位帧计数接收器

接收同一链路上跨重启、乱序投递且会自然回绕的 32 位无符号帧计数，保证旧帧不会因
计数回绕而被再次接受。后端以「最高 64 位扩展序号 + 64 位位图」维护滑动窗口，
所有裁决在同一 SQLite 事务中由窗口状态与回执记录推导，前端实时展示每次
**接受 / 重复 / 过期 / 拒绝** 裁决。

## 裁决规则

- **首次帧**：以其 32 位计数初始化窗口，位图仅置最高位。
- **纪元解绕**：取帧计数相对当前最高序号所在纪元的三个相邻纪元候选，选择距离
  最近者；两个候选距离并列（恰差 2³¹）时拒绝。
- **更高序号**：接受，位图按差值左移，更新最高扩展序号。
- **窗口内（落后 0–63）**：位图未置位则接受并置位；已置位为**重复**。
- **窗口外（落后 ≥ 64）**：**过期**，绝不因回绕计数接近而复活。
- **回执幂等**：相同回执标识 + 完全相同载荷的重传，直接返回首次裁决（即使该
  序号此刻已落在窗口外）；复用同一标识却改变链路、计数或载荷，返回 409 拒绝。
- 每帧的回执查重、窗口裁决、状态落盘都在单个 `BEGIN IMMEDIATE` 事务内完成，
  并发到达由写锁串行化；服务重启后状态完全由同一 SQLite 文件重建。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/links` | 创建链路，体：`{"link_id": "..."}` |
| `GET` | `/api/links/{link_id}` | 当前最高扩展序号、位图、最近 64 个已接受位置 |
| `POST` | `/api/links/{link_id}/frames` | 提交到达帧 |
| `GET` | `/health` | 健康检查 |

帧请求体：

```json
{ "raw_count": 4294967295, "receipt_id": "frame-0001", "payload": {"telemetry": 1} }
```

响应含 `verdict`（`accepted` / `duplicate` / `stale` / `rejected`）、
`replayed`、扩展序号（数值及 `*_dec` 十进制字符串，后者避免浏览器对
超过 2⁵³ 数值丢精度）、当前 `highest`、64 位位图与已接受位置。

## 运行（Docker Compose）

宿主端口可用 `HOST_PORT` 配置（默认 8000）：

```bash
HOST_PORT=8080 docker compose up -d --build
# 浏览器打开 http://localhost:8080
```

SQLite 数据保存在命名卷 `receiver-data` 中，容器重启后窗口与回执记录继续生效。

## 验收（verify 服务）

`verify` 服务会等待 web 健康检查通过，依次执行代码测试、构建/导入检查与实时
HTTP 冒烟（覆盖回绕、过期、重复、回执冲突），然后退出，并以退出码报告结果：

```bash
docker compose build
docker compose up --build verify      # 退出码 0 即验收通过
docker compose rm -f verify           # 清理已退出的 verify 容器
```

也可不使用容器直接运行测试：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt
cd backend && python -m pytest && python scripts/http_smoke.py
```

## 代码结构

```
backend/
├── app/
│   ├── window.py        # 纯算法：纪元解绕与 64 位位图滑动窗口
│   ├── db.py            # SQLite：窗口与回执在单事务内一致推导
│   ├── main.py          # FastAPI 接口
│   └── static/index.html
├── scripts/
│   ├── verify.sh        # 测试 + 构建检查 + HTTP 冒烟
│   └── http_smoke.py
└── tests/               # 算法、持久化/重启/并发、HTTP 测试
```
