# -*- coding: utf-8 -*-
"""
钉钉AI表格 CRUD 使用示例

运行前请确保：
1. 已安装依赖：pip install alibabacloud_dingtalk
2. 凭证可从 .config 读取，也可全部动态传入
"""

from dingtalk_ai_table import DingTalkAITable


def demo_from_config():
    """方式一：从 .config 读取全部凭证（默认）"""
    api = DingTalkAITable()
    return api


def demo_dynamic_params():
    """方式二：全部凭证动态传入，不依赖配置文件"""
    api = DingTalkAITable(
        client_id="你的client_id",
        client_secret="你的client_secret",
        base_id="AI表格文档ID",
        sheet_id_or_name="数据表名称或ID",
        operator_id="操作人unionId",
    )
    return api


def demo_mixed_params():
    """方式三：部分动态传入，其余从 .config 补全"""
    api = DingTalkAITable(
        client_id="覆盖配置中的client_id",
        operator_id="覆盖配置中的union_id",
        # client_secret / base_id / sheet_id_or_name 从 .config 读取
    )
    return api


def main():
    # 使用配置文件方式（当前项目已配置好 .config）
    api = DingTalkAITable()
    
    # ===== 1. 新增记录 =====
    new_ids = api.insert_records([
        {"标题": "测试记录A", "数字": 100},
        {"标题": "测试记录B", "数字": 200},
    ])
    print("新增记录ID:", new_ids)

    # ===== 2. 列出记录（第一页） =====
    page = api.list_records(max_results=10)
    print("本页记录数:", len(page["records"]))
    print("是否有更多:", page["hasMore"])

    # ===== 3. 获取全部记录（自动翻页） =====
    all_recs = api.list_all_records()
    print("全部记录数:", len(all_recs))

    # ===== 4. 条件筛选查询 =====
    filtered = api.list_records(
        filter_conditions=[
            {"field": "数字", "operator": "equal", "value": [100]},
        ],
    )
    print("筛选结果数:", len(filtered["records"]))

    # ===== 5. 获取单行记录 =====
    if new_ids:
        one = api.get_record(new_ids[0])
        print("单行记录:", one["id"], one["fields"])

    # ===== 6. 更新记录 =====
    if new_ids:
        updated = api.update_records([
            {"id": new_ids[0], "fields": {"数字": 999}},
        ])
        print("更新成功:", updated)

    # ===== 7. 删除记录 =====
    if new_ids:
        ok = api.delete_records(new_ids)
        print("删除成功:", ok)


if __name__ == "__main__":
    main()
