#!/usr/bin/env bash
# 重置数据库并运行归并 API 验证套件
set -eu
export PATH=/workspace/.toolchain/pg/bin:$PATH
psql -h 127.0.0.1 -p 17704 -U seal -d seal -c 'DROP SCHEMA public CASCADE; CREATE SCHEMA public;' > /dev/null
SEED_DEMO=true /workspace/.toolchain/stack.sh restart > /dev/null
sleep 4
cd /workspace/test-artifacts/gb-3105
exec /workspace/.venv/bin/python -I test_merge_api.py
