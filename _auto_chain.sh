#!/bin/bash
# 自动衔接脚本: 等待提取进程结束 → migrate → 验证 → 全量生成基本面
# 用法: nohup bash _auto_chain.sh > /tmp/auto_chain.log 2>&1 &
set -e
cd "$(dirname "$0")"
source .venv/bin/activate

EXTRACT_PID=18590
echo "[$(date +%H:%M)] 等待提取进程 PID=$EXTRACT_PID 完成..."

# 1. 等待提取进程结束 (最长等 6 小时)
WAIT=0
while ps -p $EXTRACT_PID > /dev/null 2>&1; do
  sleep 60
  WAIT=$((WAIT+1))
  if [ $WAIT -ge 360 ]; then
    echo "[$(date +%H:%M)] 等待超时 (6小时), 放弃"
    exit 1
  fi
done
echo "[$(date +%H:%M)] 提取进程已结束"

# 确认提取完整性
DONE=$(python3 -c "import sqlite3;print(sqlite3.connect('research.db').execute('SELECT count(*) FROM raw_feeds WHERE is_processed=1').fetchone()[0])")
PENDING=$(python3 -c "import sqlite3;print(sqlite3.connect('research.db').execute('SELECT count(*) FROM raw_feeds WHERE is_processed=0').fetchone()[0])")
FAILED=$(python3 -c "import sqlite3;print(sqlite3.connect('research.db').execute('SELECT count(*) FROM raw_feeds WHERE is_processed=2').fetchone()[0])")
echo "[$(date +%H:%M)] 提取结果: 已完成=$DONE 待处理=$PENDING 失败=$FAILED"

# 2. 跑 migrate 归一化迁移
echo "[$(date +%H:%M)] ═══ Step: migrate_sector_normalize ═══"
python3 migrate_sector_normalize.py

# 3. 验证数据质量
echo "[$(date +%H:%M)] ═══ Step: 数据质量验证 ═══"
python3 -c "
import sqlite3
conn = sqlite3.connect('research.db')
c = conn.cursor()
print('info_type 分布:')
for r in c.execute('SELECT info_type, count(*) FROM general_knowledge GROUP BY info_type ORDER BY count(*) DESC').fetchall():
    print(f'  {r[0]:14} {r[1]}')
print('sentiment 分布:')
for r in c.execute('SELECT sentiment, count(*) FROM sector_knowledge GROUP BY sentiment ORDER BY count(*) DESC').fetchall():
    print(f'  {r[0]:10} {r[1]}')
d = c.execute('SELECT count(DISTINCT sector) FROM sector_knowledge').fetchone()[0]
other = c.execute('SELECT count(*) FROM sector_knowledge WHERE sector=\"其他\"').fetchone()[0]
total = c.execute('SELECT count(*) FROM sector_knowledge').fetchone()[0]
print(f'distinct sector: {d} 种, 其他: {other}/{total} ({other*100//total}%)')
conn.close()
"

# 4. 全量重新生成基本面
echo "[$(date +%H:%M)] ═══ Step: 全量生成基本面 (三源融合) ═══"
python3 -u _gen_top500_fundamentals.py --force

echo "[$(date +%H:%M)] ═══ 全部完成 ═══"
