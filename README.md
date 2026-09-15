# 钉钉AI表格 Python CRUD

基于钉钉开放平台 `alibabacloud_dingtalk` SDK，实现 AI 表格（Notable）记录的增删查改。

## 安装

### 作为依赖安装到其他项目（推荐，无需复制源码）

本包是可安装的 Python 库：**pip 包名 `dingtalk-ai-table-crud`，代码导入模块名 `dingtalk_ai_table`**（两者不同）。其他项目直接安装即可引用：

```bash
# 安装最新版
pip install git+https://github.com/EdgeForge789/dingtalk-ai-table-crud.git

# 锁定到指定版本（@ 后可跟 tag / 分支 / commit）
pip install "git+https://github.com/EdgeForge789/dingtalk-ai-table-crud.git@v1.0.0"

# 升级到最新
pip install --upgrade git+https://github.com/EdgeForge789/dingtalk-ai-table-crud.git
```

在其他项目的 `requirements.txt` 里也可以直接写一行：

```
dingtalk-ai-table-crud @ git+https://github.com/EdgeForge789/dingtalk-ai-table-crud.git@v1.0.0
```

安装后直接导入，凭证在代码中动态传入、不依赖本仓库的 `.config`：

```python
from dingtalk_ai_table import DingTalkAITable
```

> 未来若发布到 PyPI，可直接 `pip install dingtalk-ai-table-crud`。

### 本机开发 / 从源码运行

克隆本仓库后安装已锁定的全部依赖：

```bash
pip install -r requirements.txt
```

如需在本机多个项目之间以“可编辑”方式引用本源码（修改即时生效、无需重装）：

```bash
pip install -e .
```

### 依赖版本

核心直接依赖为 `alibabacloud-dingtalk>=2.2.57`（模块初始化时会强制校验该版本），其余包为其传递依赖、由 pip 自动安装。当前已验证的锁定版本（见 `requirements.txt`）：

| 包 | 版本 |
|---|---|
| alibabacloud-dingtalk | 2.2.57 |
| alibabacloud-gateway-dingtalk | 1.0.2 |
| alibabacloud-tea-openapi | 0.4.6 |
| alibabacloud-tea | 0.4.3 |
| alibabacloud-tea-util | 0.3.15 |
| alibabacloud-openapi-util | 0.2.4 |
| alibabacloud-credentials | 1.0.12 |
| darabonba-core | 1.0.9 |

## 配置

项目根目录的 `.config` 文件已包含配置模板（占位符），使用前请替换为真实值。本地真实配置不会被提交（`.gitignore` 已排除）。

```ini
# 钉钉AI表配置
corp_id=你的corp_id
client_id=你的client_id
client_secret=你的client_secret
union_id=你的union_id
# 钉钉AI表文档ID
document_id=你的document_id
# 钉钉AI表数据表名称或ID
table_name=你的数据表名称或ID
```

| 字段 | 说明 | 是否必填 |
|---|---|---|
| `corp_id` | 企业 Corp ID | 否（当前未使用） |
| `client_id` | 应用 Client ID（AppKey） | 是 |
| `client_secret` | 应用 Client Secret（AppSecret） | 是 |
| `union_id` | 操作人 unionId（所有操作的默认 operator_id） | 是 |
| `document_id` | AI 表格文档 ID（baseId） | 是 |
| `table_name` | 数据表名称或 ID | 是 |

> 也可以在初始化时动态传入全部或部分凭证，无需依赖 `.config` 文件，见下方「快速使用」。

## 快速使用

### 方式一：从 .config 读取（默认）

```python
from dingtalk_ai_table import DingTalkAITable

api = DingTalkAITable()  # 自动读取同目录 .config
```

### 方式二：全部凭证动态传入

```python
from dingtalk_ai_table import DingTalkAITable

api = DingTalkAITable(
    client_id="你的client_id",
    client_secret="你的client_secret",
    base_id="AI表格文档ID",
    sheet_id_or_name="数据表名称或ID",
    operator_id="操作人unionId",
)
```

### 方式三：部分动态传入，其余从配置补全

```python
api = DingTalkAITable(
    client_id="覆盖配置的值",
    operator_id="覆盖配置的值",
    # 其余从 .config 读取
)
```

### 重试配置

```python
# 默认：失败自动重试10次，每次间隔5秒（仅对5xx/限流/网络错误重试）
api = DingTalkAITable()

# 自定义重试次数和间隔
api = DingTalkAITable(max_retries=5, retry_delay=3)

# 关闭重试
api = DingTalkAITable(max_retries=0)
```

重试日志示例：
```
[AI表] 重试 查询记录 | 第1/10次 | 等待5.0s | 错误: Error: internalError ...
```

### CRUD 操作

```python
# 新增
ids = api.insert_records([{"标题": "hello", "数字": 1}])

# 查询（分页）
page = api.list_records(max_results=50)

# 查询全部（自动翻页）
all_recs = api.list_all_records()

# 条件筛选
page = api.list_records(filter_conditions=[
    {"field": "数字", "operator": "equal", "value": [1]},
])

# 获取单行
rec = api.get_record(ids[0])

# 更新
api.update_records([{"id": ids[0], "fields": {"数字": 2}}])

# 删除
api.delete_records(ids)
```

### 查重新增（只新增不存在的数据）

`insert_records_if_not_exists` 会先拉取目标表全部记录构建指纹，跳过已存在的记录、只新增不存在的；同一批次内部的重复数据也会自动去重。通过 `dedup_fields` 指定按哪些列查重：

```python
# 1) 默认：对比全部列（所有字段值都相同才算重复）
result = api.insert_records_if_not_exists([
    {"编号": "A001", "标题": "商品A", "数量": 10},
    {"编号": "A002", "标题": "商品B", "数量": 20},
])
# result = {"inserted_ids": [...], "inserted_count": N,
#           "skipped_count": M, "skipped_indexes": [被跳过记录在输入中的下标]}

# 2) 按单个字段查重（如只看“编号”，编号相同即跳过，忽略其他字段差异）
result = api.insert_records_if_not_exists(records, dedup_fields=["编号"])

# 3) 多字段组合查重（这些字段值都相同才算重复）
result = api.insert_records_if_not_exists(records, dedup_fields=["部门", "姓名"])

# 异步版本
result = await api.insert_records_if_not_exists_async(records, dedup_fields=["编号"])
```

> 查重时已自动对齐钉钉读回格式差异：数字字段读回为字符串（`10` 与 `"10"` 视为相同）、多选字段读回为对象数组且顺序可能不同（`["红","蓝"]` 与 `[{"name":"蓝"},{"name":"红"}]` 视为相同）、空值字段读回时键缺失（`[]`/`""` 与无此键视为相同）。
> 注意：该方法需先全表扫描一次，超大表（数万行以上）查重耗时主要在拉取已有记录。

### 异步调用（async/await，支持并发）

```python
import asyncio
from dingtalk_ai_table import DingTalkAITable

api = DingTalkAITable()

async def main():
    # 异步CRUD（方法名加 _async 后缀）
    ids = await api.insert_records_async([{"标题": "hello"}])
    page = await api.list_records_async(max_results=50)
    rec = await api.get_record_async(ids[0])
    await api.update_records_async([{"id": ids[0], "fields": {"标题": "hi"}}])
    await api.delete_records_async(ids)

    # 并发执行多个请求
    results = await asyncio.gather(
        api.list_records_async(max_results=10),
        api.get_record_async("rec_xxx"),
        api.list_records_async(filter_conditions=[...]),
    )

asyncio.run(main())
```

异步方法列表：`insert_records_async` / `list_records_async` / `list_all_records_async` / `get_record_async` / `update_records_async` / `delete_records_async` / `insert_records_if_not_exists_async`。token 缓存线程安全，多协程共享不会重复获取。

## API 说明

| 方法 | 对应钉钉接口 | 说明 |
|---|---|---|
| `insert_records(records)` | 新增记录 | 批量新增，返回记录ID列表 |
| `insert_records_if_not_exists(records, dedup_fields)` | 新增记录 | 查重后仅新增不存在的数据，返回新增ID/跳过数/跳过下标 |
| `list_records(...)` | 列出多行记录 | 分页查询，支持筛选 |
| `list_all_records(...)` | 列出多行记录 | 自动翻页返回全部 |
| `get_record(record_id)` | 获取记录 | 按ID查单行 |
| `update_records(records)` | 更新多行记录 | 按ID批量更新字段 |
| `delete_records(record_ids)` | 删除多行记录 | 按ID批量删除 |
| `create_field(name, field_type)` | 创建字段 | 新建一列，支持 property 属性 |
| `list_fields()` | 获取所有字段 | 返回字段列表（id/name/type/property） |
| `update_field(field_id_or_name, name)` | 更新字段 | 重命名字段或更新属性 |
| `delete_field(field_id_or_name)` | 删除字段 | 按ID或名称删除一列 |
| `create_sheet(name, fields)` | 创建数据表 | 在Base下新建数据表，可带初始字段 |
| `get_sheet(sheet_id_or_name)` | 获取数据表 | 按ID或名称查单个数据表 |
| `list_sheets()` | 获取所有数据表 | 返回Base下全部数据表列表 |
| `update_sheet(sheet_id_or_name, name)` | 更新数据表 | 重命名数据表 |
| `delete_sheet(sheet_id_or_name)` | 删除数据表 | 按ID或名称删除数据表（不可恢复） |

### 字段管理（Field CRUD）

```python
# 获取所有字段
fields = api.list_fields()
# [{"id": "xxx", "name": "标题", "type": "text", "property": None}, ...]

# 创建字段（text 类型）
field = api.create_field(name="备注", field_type="text")

# 创建单选字段（带选项）
field = api.create_field(
    name="优先级",
    field_type="singleSelect",
    property={"options": [{"name": "高"}, {"name": "中"}, {"name": "低"}]},
)

# 更新字段（重命名）
api.update_field(field_id_or_name="备注", name="备注信息")

# 删除字段
api.delete_field("备注信息")
```

> 注意：第一个字段（主字段）不可删除；更新字段不支持修改字段类型。

异步方法：`create_field_async` / `list_fields_async` / `update_field_async` / `delete_field_async`。

### 数据表管理（Sheet CRUD）

在 AI 表格文档（Base）下管理多个数据表。

```python
# 获取所有数据表
sheets = api.list_sheets()
# [{"id": "stxxx", "name": "数据表1"}, ...]

# 创建数据表（仅名称）
sheet = api.create_sheet(name="新数据表")

# 创建数据表（带初始字段）
sheet = api.create_sheet(
    name="订单表",
    fields=[
        {"name": "订单号", "type": "text"},
        {"name": "金额", "type": "number"},
        {"name": "状态", "type": "singleSelect", "property": {"options": [{"name": "待付款"}, {"name": "已完成"}]}},
    ],
)

# 获取单个数据表信息
sheet = api.get_sheet("stxxx")  # 或按名称 api.get_sheet("订单表")

# 更新数据表（重命名）
api.update_sheet("stxxx", name="订单表_v2")

# 删除数据表（不可恢复，请谨慎操作）
api.delete_sheet("stxxx")
```

> 注意：`create_sheet` 和 `list_sheets` 操作的是整个 AI 表格文档，不依赖初始化时指定的 `sheet_id_or_name`；删除数据表后数据不可恢复。

异步方法：`create_sheet_async` / `get_sheet_async` / `list_sheets_async` / `update_sheet_async` / `delete_sheet_async`。

## 注意事项

- **operator_id（unionId）必填**：所有记录操作都需要操作人 unionId，可在初始化时传入或调用时传入。
- **access_token 自动缓存**：有效期 7200 秒，模块内自动缓存续期，无需手动管理。
- **字段值格式**：不同字段类型的值格式不同（如日期为毫秒时间戳、人员为 `[{"unionId": "..."}]`），参考钉钉文档「记录值格式」。
- **筛选操作符**：`equal` / `notEqual` / `greaterThan` / `lessThan` / `contains` 等，具体以钉钉文档为准。支持符号简写：`>`、`>=`、`<`、`<=`、`=`、`!=`、`contain`、`not_contain` 自动转换。
- **失败自动重试**：遇到 5xx 服务端错误、429 限流或网络异常时自动重试，默认重试10次、每次间隔5秒，通过 `max_retries` 和 `retry_delay` 参数调整。4xx 参数错误不重试。
- **calc_fields**：`list_records(calc_fields=True)` 可返回计算字段和查找引用字段的值。
- **SDK 版本检查**：初始化时自动校验 `alibabacloud-dingtalk >= 2.2.57`，版本过低会抛 `ImportError`。
- **单批数量与自动拆分**：新增记录每批最多200条，更新/删除每批最多100条，超出自动拆分多批执行并合并结果；查询（`list_records`）默认每页100条，`list_all_records` 按每页100条自动翻页拉取全部。

### 多选字段（multiSelect）

多选字段的值为字符串数组，写入和更新时直接传选项名称数组：

```python
# 写入/更新：传选项名称数组
api.insert_records([{"标签": ["办公电脑", "移动设备"]}])
api.update_records([{"id": "rec_xxx", "fields": {"标签": ["网络设备"]}}])

# 清空：传空数组
api.update_records([{"id": "rec_xxx", "fields": {"标签": []}}])

# 读取：返回对象数组（含 name 和 id）
rec = api.get_record("rec_xxx")
# rec["fields"]["标签"] = [{"name": "办公电脑", "id": "xxx"}, {"name": "移动设备", "id": "yyy"}]
```

筛选限制：多选字段仅支持 `equal` / `notEqual`（完全匹配整个多选值），不支持 `contains` 包含匹配。
