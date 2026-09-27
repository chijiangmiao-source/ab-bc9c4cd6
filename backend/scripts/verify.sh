#!/bin/sh
# verify 服务入口：代码测试 -> 构建/导入检查 -> 实时 HTTP 冒烟。
# 任一步失败即以非零退出码结束，docker compose run verify 直接报告验收结果。
set -e

echo "== 1/3 代码测试（回绕、过期、重复、回执、重启、并发） =="
python -m pytest

echo "== 2/3 构建/导入检查 =="
python -m compileall -q app scripts
python -c "import app.main; print('应用模块导入成功')"

echo "== 3/3 针对 web 服务的 HTTP 冒烟 =="
python scripts/http_smoke.py

echo ""
echo "全部验收检查通过。"
