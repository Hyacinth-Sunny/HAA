#!/usr/bin/env bash
# 启动 HAA Web 服务（FastAPI 一体：API + 页面 + 静态资源）。
# 自动用 haa conda env 的 python，监听 config/default.yaml 里的 0.0.0.0:8420。
#
# 用法：
#   ./run_server.sh                       # 直接起，浏览器开 http://localhost:8420
#   DEEPSEEK_API_KEY=... TAVILY_API_KEY=... ./run_server.sh   # 带 key 起（Run campaign 时才需要）
#
# 停止：Ctrl+C
# 若报 "address already in use"：先杀占用 8420 的旧进程
#   lsof -ti:8420 | xargs -r kill -9
set -e
cd "$(dirname "$0")"
exec /home/hyacinth-sunny/anaconda3/envs/haa/bin/python -m api.server "$@"
