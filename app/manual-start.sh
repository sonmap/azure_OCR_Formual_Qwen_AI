#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

case "${1:-base}" in
  base)
    docker compose up -d --no-build formula-ocr backend
    for i in {1..30}; do
      if curl -fsS http://localhost:8000/health >/dev/null; then
        break
      fi
      sleep 1
    done
    curl -fsS http://localhost:8000/health >/dev/null
    docker compose up -d --no-build frontend
    ;;
  qwen)
    docker compose --profile qwen up -d --no-build qwen-vl formula-ocr backend
    for i in {1..30}; do
      if curl -fsS http://localhost:8000/health >/dev/null; then
        break
      fi
      sleep 1
    done
    curl -fsS http://localhost:8000/health >/dev/null
    docker compose --profile qwen up -d --no-build frontend
    ;;
  restart-backend)
    docker compose --profile qwen stop frontend
    docker compose --profile qwen up -d --no-build backend
    for i in {1..30}; do
      if curl -fsS http://localhost:8000/health >/dev/null; then
        break
      fi
      sleep 1
    done
    curl -fsS http://localhost:8000/health >/dev/null
    docker compose --profile qwen up -d --no-build frontend
    ;;
  stop)
    docker compose --profile qwen stop frontend backend qwen-vl formula-ocr
    ;;
  status)
    docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E 'NAMES|actuarial|qwen'
    ;;
  *)
    echo "Usage: $0 {base|qwen|restart-backend|stop|status}" >&2
    exit 2
    ;;
esac

docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}' | grep -E 'NAMES|actuarial|qwen'
