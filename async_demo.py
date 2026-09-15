# -*- coding: utf-8 -*-
"""异步方法真实API验证：并发查询 + 异步CRUD"""
import asyncio, time
from dingtalk_ai_table import DingTalkAITable

api = DingTalkAITable()

async def main():
    # 1. 并发查询3次（对比同步串行耗时）
    print("=== 异步并发查询3次 ===")
    t0 = time.time()
    results = await asyncio.gather(
        api.list_records_async(max_results=5),
        api.list_records_async(max_results=5),
        api.list_records_async(max_results=5),
    )
    total = (time.time() - t0) * 1000
    print("并发3次总耗时: %.1fms" % total)

    # 2. 异步新增
    print("\n=== 异步新增 ===")
    ids = await api.insert_records_async([
        {"资产名称": "异步测试_A", "编号": "ASY-001"},
        {"资产名称": "异步测试_B", "编号": "ASY-002"},
    ])

    # 3. 并发获取2条记录
    if len(ids) >= 2:
        print("\n=== 并发获取2条单行 ===")
        recs = await asyncio.gather(
            api.get_record_async(ids[0]),
            api.get_record_async(ids[1]),
        )
        print("获取到 %d 条" % len(recs))

    # 4. 异步更新
    print("\n=== 异步更新 ===")
    if ids:
        await api.update_records_async([{"id": ids[0], "fields": {"资产名称": "异步测试_A_已更新"}}])

    # 5. 异步删除
    print("\n=== 异步删除 ===")
    if ids:
        await api.delete_records_async(ids)

    print("\n异步验证完成，测试数据已清理。")

asyncio.run(main())
