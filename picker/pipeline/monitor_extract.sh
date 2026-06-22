#!/usr/bin/env bash
# 自动续跑研报提取 (macOS 兼容, 无 setsid 依赖)
# 策略: 每轮提取限时 480s (8分钟) 后干净退出, monitor 每分钟检查并重启。
# 提取是增量断点续跑 (is_processed=0 的才处理), 多轮叠加直到全部完成。
set -u
cd /Users/bilibili/Desktop/J-TradingAgents
LOG=/tmp/backfill_extract.log
ROUND=0

while true; do
    UNPROC=$(uv run python3 -c "
import sqlite3
print(sqlite3.connect('research.db').execute('SELECT COUNT(*) FROM raw_feeds WHERE is_processed=0').fetchone()[0])
" 2>/dev/null)
    if [ "$UNPROC" = "0" ] || [ -z "$UNPROC" ]; then
        echo "[$(date +%H:%M:%S)] ✅ 全部处理完成 (unproc=0), 退出监控"
        break
    fi
    if pgrep -f "backfill_research.py --step 2" > /dev/null; then
        echo "[$(date +%H:%M:%S)] 运行中 (round $ROUND) | 待处理 ${UNPROC} 帖"
    else
        ROUND=$((ROUND + 1))
        echo "[$(date +%H:%M:%S)] 启动第 $ROUND 轮提取 (待处理 ${UNPROC} 帖, 限时480s)"
        # 前台跑 (限时8分钟自退出), 不用 nohup/setsid
        env PYTHONUNBUFFERED=1 uv run python3 -u picker/pipeline/backfill_research.py \
            --step 2 --workers 8 --time-limit 480 >> "$LOG" 2>&1
        echo "[$(date +%H:%M:%S)] 第 $ROUND 轮结束"
    fi
    sleep 30
done
