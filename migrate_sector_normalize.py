#!/usr/bin/env python3
"""存量 sector 归一化迁移脚本。

背景:
  全量重提取的后台进程在扩充映射表之前就已启动, 用的是旧 prompt,
  导致传统行业 (油气/化工/金融等) 仍被标成"其他"。
  本脚本对已入库的 sector_knowledge 做一次性归一化:
    1. 先用 normalize.py 映射表归类 (能直接映射的)
    2. 对仍是"其他"的, 用 viewpoint 文本做关键词匹配兜底

使用:
  uv run python3 migrate_sector_normalize.py --dry-run   # 预览, 不写库
  uv run python3 migrate_sector_normalize.py             # 执行迁移
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import sqlite3
from tradingagents.research.normalize import normalize_sector, ALLOWED_SECTORS

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'research.db')

# viewpoint 关键词 → 标准赛道 (兜底分类, 仅对"其他"使用)
_VIEWPOINT_KEYWORD_MAP = [
    (['油气', '原油', '石油', '煤炭', '天然气', '旧能源', '传统能源', '燃油', '油价'], '油气煤炭'),
    (['化工', '化学', '化肥', '聚氨酯', '钛白粉', '维生素', '染料', '磷化工'], '化工'),
    (['银行', '保险', '证券', '券商', '金融', '多元金融'], '大金融'),
    (['猪肉', '生猪', '养殖', '农业', '种业', '饲料', '奶牛', '种植'], '农业'),
    (['海运', '油运', '航运', '港口', '物流', '高铁', '快递', '航空', '交运'], '交运/海运'),
    (['互联网', '游戏', '传媒', '白酒', '食品', '家电', '电商', '消费', '教育', '纺织'], '消费/互联网'),
    (['光模块', '光通信', 'CPO', '算力', '硅光', '光纤'], '光通信/AI算力'),
    (['存储', 'HBM', '存储芯片'], '存储/HBM'),
    (['稀土', '战略金属', '稀有金属', '锗', '镓', '铟'], '战略金属'),
    (['机器人', '人形', '物理AI'], '机器人/物理AI'),
    (['固态电池', '钠离子'], '固态电池'),
    (['商业航天', 'SpaceX', '卫星', '大飞机'], '商业航天'),
    (['创新药', '医药', '生物', 'CRO'], '创新药'),
    (['半导体', '光刻', '芯片', '硅片', '碳化硅', '靶材'], '半导体设备/材料'),
    (['锂电', '电池', '储能', '光伏', '电解液', '隔膜'], '锂电/新能源'),
    (['贵金属', '黄金', '白银', '有色', '矿产', '铝'], '贵金属/有色'),
    (['军工', '航空锻件', '国防'], '军工'),
]


def classify_by_viewpoint(viewpoint: str):
    """用 viewpoint 关键词匹配, 返回标准赛道或 None。"""
    if not viewpoint:
        return None
    for keywords, sector in _VIEWPOINT_KEYWORD_MAP:
        if any(kw in viewpoint for kw in keywords):
            return sector
    return None


def migrate(dry_run=True):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    rows = c.execute('SELECT id, feed_id, sector, viewpoint FROM sector_knowledge').fetchall()
    total = len(rows)
    other_after = 0
    reclassified = {}  # new_sector -> count

    # 第 1 步: 计算每条记录归一化后的新 sector
    planned = []  # (id, feed_id, old_sector, new_sector)
    for r in rows:
        old = r['sector']
        vp = r['viewpoint'] or ''
        new = normalize_sector(old)
        # 仍是"其他"/未命中, 用 viewpoint 兜底
        if new == '其他' or (new not in ALLOWED_SECTORS and new == old):
            vp_result = classify_by_viewpoint(vp)
            if vp_result:
                new = vp_result
        planned.append((r['id'], r['feed_id'], old, new))
        if new != old:
            reclassified[new] = reclassified.get(new, 0) + 1
        if new == '其他':
            other_after += 1

    # 第 2 步: 检测归一化后会冲突的记录 (同 feed_id 内归并到相同 new_sector)
    # sector_knowledge 有 UNIQUE(feed_id, sector) 约束, 直接 UPDATE 会冲突
    from collections import defaultdict
    by_feed = defaultdict(list)  # feed_id -> [(id, old, new)]
    for pid, fid, old, new in planned:
        by_feed[fid].append((pid, old, new))

    delete_ids = []   # 冲突记录中要删除的 (保留 viewpoint 最长的一条)
    update_list = []  # (new_sector, id) 安全的更新
    for fid, items in by_feed.items():
        # 按 new_sector 分组
        by_new = defaultdict(list)
        for pid, old, new in items:
            by_new[new].append((pid, old))
        for new_sector, id_pairs in by_new.items():
            if len(id_pairs) > 1:
                # 同 feed 归并到同 sector: 保留第一条, 其余删除
                keep_id = id_pairs[0][0]
                for pid, old in id_pairs[1:]:
                    delete_ids.append(pid)
                update_list.append((new_sector, keep_id))
            else:
                pid, old = id_pairs[0]
                update_list.append((new_sector, pid))

    # 只保留实际发生变化的更新
    old_map = {p[0]: p[2] for p in planned}
    update_list = [(new, pid) for new, pid in update_list if new != old_map[pid]]

    print(f'═══ sector 归一化迁移 {"[DRY-RUN]" if dry_run else "[执行]"} ═══')
    print(f'总 sector_knowledge 记录: {total}')
    print(f'将更新 sector: {len(update_list)} 条')
    print(f'将删除(同帖归并重复): {len(delete_ids)} 条')
    print(f'迁移后"其他"剩余: {other_after} 条 ({other_after*100//total}%)')
    print()
    print('重新归类分布:')
    for sector, cnt in sorted(reclassified.items(), key=lambda x: -x[1]):
        print(f'  → {sector:16} {cnt} 条')

    if not dry_run and (update_list or delete_ids):
        # 先删除冲突记录, 再更新
        if delete_ids:
            c.executemany('DELETE FROM sector_knowledge WHERE id=?', [(i,) for i in delete_ids])
        if update_list:
            c.executemany('UPDATE sector_knowledge SET sector=? WHERE id=?', update_list)
        conn.commit()
        print(f'\n✓ 已更新 {len(update_list)} 条, 删除 {len(delete_ids)} 条冲突记录')

    # 迁移后完整分布
    print(f'\n{"迁移后" if not dry_run else "预计迁移后"} sector 分布:')
    for r in c.execute('SELECT sector, count(*) FROM sector_knowledge GROUP BY sector ORDER BY count(*) DESC').fetchall():
        print(f'  {r["sector"]:20} {r[1]}')

    conn.close()


if __name__ == '__main__':
    dry = '--dry-run' in sys.argv
    migrate(dry_run=dry)
