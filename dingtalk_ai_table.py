# -*- coding: utf-8 -*-
"""
钉钉AI表格（Notable）服务端 API —— 增删查改基础封装

依赖：pip install alibabacloud_dingtalk
配置：同目录下 .config 文件，格式 key=value，包含：
    corp_id, client_id, client_secret, document_id, table_name
    （可选）operator_id
"""

import os
import json
import time
import asyncio
import threading
import configparser
from typing import Any, Dict, List, Optional, Tuple

from importlib.metadata import version as _pkg_version, PackageNotFoundError

from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models
from alibabacloud_dingtalk.oauth2_1_0 import client as oauth_client
from alibabacloud_dingtalk.oauth2_1_0 import models as oauth_models
from alibabacloud_dingtalk.notable_1_0 import client as notable_client
from alibabacloud_dingtalk.notable_1_0 import models as notable_models


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

MIN_SDK_VERSION = "2.2.57"
INSERT_BATCH_SIZE = 200   # 新增记录每批最大200条
UPDATE_BATCH_SIZE = 100   # 更新记录每批最大100条
DELETE_BATCH_SIZE = 100   # 删除记录每批最大100条


# ---------------------------------------------------------------------------
# 筛选操作符映射（用户友好符号 → 钉钉API操作符）
# ---------------------------------------------------------------------------

OPERATOR_MAP: Dict[str, str] = {
    # 等于
    "=": "equal",
    "==": "equal",
    "equal": "equal",
    "eq": "equal",
    # 不等于
    "!=": "notEqual",
    "<>": "notEqual",
    "notEqual": "notEqual",
    "ne": "notEqual",
    # 大于
    ">": "greaterThan",
    "greaterThan": "greaterThan",
    "gt": "greaterThan",
    # 大于等于
    ">=": "greaterThanOrEqual",
    "greaterThanOrEqual": "greaterThanOrEqual",
    "greaterEqual": "greaterThanOrEqual",
    "gte": "greaterThanOrEqual",
    # 小于
    "<": "lessThan",
    "lessThan": "lessThan",
    "lt": "lessThan",
    # 小于等于
    "<=": "lessThanOrEqual",
    "lessThanOrEqual": "lessThanOrEqual",
    "lessEqual": "lessThanOrEqual",
    "lte": "lessThanOrEqual",
    # 包含
    "contain": "contains",
    "contains": "contains",
    "like": "contains",
    # 不包含
    "not_contain": "notContains",
    "notContains": "notContains",
    "notLike": "notContains",
}


def normalize_operator(op: str) -> str:
    """将用户友好的操作符符号转换为钉钉API操作符。

    支持: >, >=, <, <=, =, ==, !=, <>, contain, contains, not_contain, notContains
    以及钉钉原生操作符（原样透传）。
    """
    if not op:
        return "equal"
    key = op.strip()
    return OPERATOR_MAP.get(key, key)  # 未匹配时原样透传（兼容原生操作符）


# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------

def load_config(config_path: Optional[str] = None) -> Dict[str, str]:
    """从 .config 文件加载配置，返回纯字典（不打印任何内容）。"""
    if config_path is None:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".config")

    parser = configparser.ConfigParser()
    # configparser 需要一个 section，给无 section 的纯 key=value 文件补一个虚拟段
    with open(config_path, "r", encoding="utf-8") as f:
        parser.read_string("[DEFAULT]\n" + f.read())

    cfg = {k: v.strip() for k, v in parser["DEFAULT"].items()}
    return cfg


# ---------------------------------------------------------------------------
# AccessToken 管理（按 client_id 维度缓存，有效期 7200 秒，线程安全）
# ---------------------------------------------------------------------------

_token_cache: Dict[str, Dict[str, Any]] = {}
_token_lock = threading.Lock()  # 多线程/多协程共享缓存的锁


def get_access_token(client_id: str, client_secret: str, force_refresh: bool = False) -> str:
    """获取企业内部应用 access_token，按 client_id 自动缓存（线程安全）。"""
    now = time.time()
    # 先无锁快速检查缓存
    cached = _token_cache.get(client_id)
    if not force_refresh and cached and now < cached["expire_at"] - 60:
        return cached["token"]

    with _token_lock:
        # 双重检查，避免多线程重复获取
        now = time.time()
        cached = _token_cache.get(client_id)
        if not force_refresh and cached and now < cached["expire_at"] - 60:
            return cached["token"]

        config = open_api_models.Config(protocol="https", region_id="central")
        cli = oauth_client.Client(config)
        req = oauth_models.GetAccessTokenRequest(app_key=client_id, app_secret=client_secret)
        resp = cli.get_access_token(req)

        token = resp.body.access_token
        _token_cache[client_id] = {
            "token": token,
            "expire_at": now + (resp.body.expire_in or 7200),
        }
        return token


async def get_access_token_async(client_id: str, client_secret: str, force_refresh: bool = False) -> str:
    """异步获取企业内部应用 access_token，按 client_id 自动缓存（线程安全）。"""
    now = time.time()
    cached = _token_cache.get(client_id)
    if not force_refresh and cached and now < cached["expire_at"] - 60:
        return cached["token"]

    # token 获取是 IO 密集型，放到线程池避免阻塞事件循环
    return await asyncio.to_thread(get_access_token, client_id, client_secret, force_refresh)


def _build_notable_client() -> notable_client.Client:
    """构建 AI 表客户端（基础配置，鉴权头由每次请求显式携带）。"""
    config = open_api_models.Config(
        protocol="https",
        region_id="central",
    )
    return notable_client.Client(config)


def _is_retryable_error(e: Exception) -> bool:
    """判断异常是否可重试（5xx、限流、网络错误）。"""
    # 网络层错误
    if isinstance(e, (ConnectionError, TimeoutError, OSError)):
        return True
    # 钉钉SDK异常
    status_code = getattr(e, "statusCode", None)
    if status_code is not None:
        return status_code >= 500 or status_code == 429
    # 按错误码判断
    code = getattr(e, "code", "")
    if code in ("service.timeout", "internalError", "unknownError", "TooManyRequests"):
        return True
    return False


def _parse_version(v: str) -> Tuple[int, ...]:
    """将版本字符串解析为整数元组，用于比较。"""
    parts = []
    for p in v.strip().split("."):
        # 取数字部分（如 "2.2.57" -> (2,2,57)，"2.2.57rc1" -> (2,2,57)）
        num = ""
        for ch in p:
            if ch.isdigit():
                num += ch
            else:
                break
        parts.append(int(num) if num else 0)
    return tuple(parts)


def _check_sdk_version():
    """检查 alibabacloud-dingtalk 版本 >= MIN_SDK_VERSION，否则抛 ImportError。"""
    try:
        installed = _pkg_version("alibabacloud-dingtalk")
    except PackageNotFoundError:
        raise ImportError(
            "未安装 alibabacloud-dingtalk，请执行: pip install alibabacloud-dingtalk>=%s"
            % MIN_SDK_VERSION
        )
    if _parse_version(installed) < _parse_version(MIN_SDK_VERSION):
        raise ImportError(
            "alibabacloud-dingtalk 版本过低: 当前 %s，需要 >= %s。请执行: "
            "pip install --upgrade alibabacloud-dingtalk==%s"
            % (installed, MIN_SDK_VERSION, MIN_SDK_VERSION)
        )


def _chunk_list(items: List[Any], batch_size: int) -> List[List[Any]]:
    """将列表按 batch_size 拆分为多个子列表。"""
    if batch_size <= 0:
        return [items]
    return [items[i:i + batch_size] for i in range(0, len(items), batch_size)]


def _normalize_dedup_value(value: Any) -> Any:
    """规范化单个字段值，消除「写入值」与「读回值」的格式差异，便于查重比较。

    钉钉 AI 表读回时存在以下格式差异，此处统一拉齐：
    - number 写入为 int/float，读回为字符串（10 → "10"）→ 数字统一为字符串；
    - 多选写入为字符串数组，读回为 [{"name":..,"id":..}] 对象数组 → 提取名称并排序；
    - 人员等对象数组同理（优先 name，其次 unionId）；
    - 空数组 / 空字符串 / None 统一归为空（返回 None，构建指纹时剔除，
      与「读回时空值字段键直接缺失」的行为对齐）；
    - 字典 → 排序后的 JSON 字符串。
    """
    if value is None:
        return None
    # bool 是 int 子类，需先排除
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and float(value).is_integer():
            return str(int(value))
        return str(value)
    if isinstance(value, list):
        if not value:
            return None
        normalized = []
        for item in value:
            if isinstance(item, dict):
                normalized.append(
                    item.get("name")
                    or item.get("unionId")
                    or json.dumps(item, sort_keys=True, ensure_ascii=False, default=str)
                )
            else:
                normalized.append(_normalize_dedup_value(item))
        return sorted(normalized, key=lambda x: str(x))
    if isinstance(value, dict):
        return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    if isinstance(value, str) and value.strip() == "":
        return None
    return value


def _make_dedup_fingerprint(
    record: Dict[str, Any],
    dedup_fields: Optional[List[str]] = None,
) -> str:
    """构建一条记录的查重指纹（可哈希比较的字符串）。

    :param record: 记录字段字典
    :param dedup_fields: 参与查重的字段名列表；
        None 表示用全部字段，指定时只取这些字段（缺失字段按空处理）。
        规范化后为空值（None/空串/空数组）的字段会被剔除，
        以对齐「写入空值、读回时该键缺失」造成的差异。
    """
    raw = {f: record.get(f) for f in dedup_fields} if dedup_fields else dict(record)
    subset: Dict[str, Any] = {}
    for k, v in raw.items():
        nv = _normalize_dedup_value(v)
        if nv is None:
            continue
        subset[k] = nv
    return json.dumps(subset, sort_keys=True, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# 核心类
# ---------------------------------------------------------------------------

class DingTalkAITable:
    """钉钉AI表格 CRUD 封装。

    凭证优先级：显式传入参数 > .config 配置文件。
    所有参数均可动态传入，不依赖配置文件。
    """

    def __init__(
        self,
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
        base_id: Optional[str] = None,
        sheet_id_or_name: Optional[str] = None,
        operator_id: Optional[str] = None,
        config_path: Optional[str] = None,
        max_retries: int = 10,
        retry_delay: float = 5,
    ):
        """
        :param client_id: 应用 Client ID（不传则读配置）
        :param client_secret: 应用 Client Secret（不传则读配置）
        :param base_id: AI表格文档ID（不传则读配置 document_id）
        :param sheet_id_or_name: 数据表ID或名称（不传则读配置 table_name）
        :param operator_id: 操作人 unionId（不传则读配置 operator_id / union_id）
        :param config_path: 配置文件路径，默认同目录 .config；设为 None 且所有参数都传时不读配置
        :param max_retries: 失败重试次数（仅对5xx/限流/网络错误重试），默认10次
        :param retry_delay: 每次重试之间的等待秒数，默认5秒
        :raises ImportError: alibabacloud-dingtalk 版本低于 %s 时抛出
        """
        # 版本检查：SDK 版本必须 >= 2.2.57
        _check_sdk_version()

        # 仅当有参数缺失时才尝试读配置文件
        cfg: Dict[str, str] = {}
        need_config = any(v is None for v in (
            client_id, client_secret, base_id, sheet_id_or_name, operator_id
        ))
        if need_config:
            try:
                cfg = load_config(config_path)
            except FileNotFoundError:
                cfg = {}

        self.client_id = client_id or cfg.get("client_id")
        self.client_secret = client_secret or cfg.get("client_secret")
        self.base_id = base_id or cfg.get("document_id")
        self.sheet_id_or_name = sheet_id_or_name or cfg.get("table_name")
        self.operator_id = (
            operator_id
            or cfg.get("operator_id")
            or cfg.get("union_id")
        )

        # 校验必填项
        missing = []
        if not self.client_id:
            missing.append("client_id")
        if not self.client_secret:
            missing.append("client_secret")
        if not self.base_id:
            missing.append("base_id")
        if not self.sheet_id_or_name:
            missing.append("sheet_id_or_name")
        if missing:
            raise ValueError(
                f"缺少必填凭证: {', '.join(missing)}，请显式传入或在 .config 中配置"
            )
        self.max_retries = max(0, int(max_retries))
        self.retry_delay = max(0, float(retry_delay))

    # -- 内部工具 ----------------------------------------------------------

    def _client_and_token(self):
        """获取 AI 表客户端和当前有效的 access_token。"""
        token = get_access_token(self.client_id, self.client_secret)
        return _build_notable_client(), token

    def _runtime(self) -> util_models.RuntimeOptions:
        """构建运行时选项。"""
        return util_models.RuntimeOptions()

    def _log(self, action: str, elapsed_ms: float, detail: str = ""):
        """统一日志输出：表名 + 操作 + 耗时 + 详情。"""
        msg = "[AI表] %s | 表=%s | 耗时=%.1fms" % (action, self.sheet_id_or_name, elapsed_ms)
        if detail:
            msg += " | " + detail
        print(msg)

    def _retry_call(self, action: str, func):
        """同步重试执行：遇到可重试异常时自动重试，指数退避。

        :param action: 操作名称（用于日志）
        :param func: 无参可调用对象，执行实际API调用
        :return: func 的返回值
        """
        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                return func()
            except Exception as e:
                last_exc = e
                if attempt >= self.max_retries or not _is_retryable_error(e):
                    raise
                wait = self.retry_delay
                err_msg = str(e)[:120]
                print("[AI表] 重试 %s | 第%d/%d次 | 等待%.1fs | 错误: %s" % (
                    action, attempt + 1, self.max_retries, wait, err_msg))
                time.sleep(wait)
        raise last_exc  # pragma: no cover

    async def _retry_call_async(self, action: str, coro_func):
        """异步重试执行：遇到可重试异常时自动重试，指数退避。

        :param action: 操作名称（用于日志）
        :param coro_func: 无参异步可调用对象，执行实际API调用
        :return: coro_func 的返回值
        """
        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                return await coro_func()
            except Exception as e:
                last_exc = e
                if attempt >= self.max_retries or not _is_retryable_error(e):
                    raise
                wait = self.retry_delay
                err_msg = str(e)[:120]
                print("[AI表] 异步重试 %s | 第%d/%d次 | 等待%.1fs | 错误: %s" % (
                    action, attempt + 1, self.max_retries, wait, err_msg))
                await asyncio.sleep(wait)
        raise last_exc  # pragma: no cover

    def _require_operator(self, operator_id: Optional[str]) -> str:
        op = operator_id or self.operator_id
        if not op:
            raise ValueError("缺少 operator_id（操作人 unionId），请在初始化或调用时传入")
        return op

    # -- 增：新增多行记录 --------------------------------------------------

    def _insert_batch(
        self,
        records: List[Dict[str, Any]],
        operator_id: str,
        client_token: Optional[str] = None,
    ) -> List[str]:
        """单批新增（不超过 INSERT_BATCH_SIZE 条）。"""
        cli, token = self._client_and_token()
        req_records = [
            notable_models.InsertRecordsRequestRecords(fields=rec)
            for rec in records
        ]
        req = notable_models.InsertRecordsRequest(
            operator_id=operator_id,
            records=req_records,
            client_token=client_token,
        )
        headers = notable_models.InsertRecordsHeaders(
            x_acs_dingtalk_access_token=token,
        )
        _t0 = time.time()
        resp = self._retry_call("新增记录", lambda: cli.insert_records_with_options(
            self.base_id, self.sheet_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        ids = [item.id for item in (resp.body.value or [])]
        self._log("新增记录", _elapsed,
                  "本批输入%d条, 成功%d条" % (len(records), len(ids)))
        return ids

    def insert_records(
        self,
        records: List[Dict[str, Any]],
        operator_id: Optional[str] = None,
        client_token: Optional[str] = None,
    ) -> List[str]:
        """
        新增多行记录（自动分批，每批最多 %d 条）。

        请求头自动携带 x-acs-dingtalk-access-token；
        URL 查询参数自动携带 operatorId。

        :param records: [{"字段名": 值, ...}, ...]
        :param operator_id: 操作人 unionId（可选，默认用初始化时的）
        :param client_token: 幂等键 UUID v4（可选，仅第一批生效）
        :return: 新增成功的记录 ID 列表
        """ % INSERT_BATCH_SIZE
        op = self._require_operator(operator_id)
        if not records:
            return []
        batches = _chunk_list(records, INSERT_BATCH_SIZE)
        all_ids: List[str] = []
        _t0 = time.time()
        for i, batch in enumerate(batches):
            # 仅第一批传 client_token（幂等键），后续批次不传避免冲突
            ct = client_token if i == 0 else None
            all_ids.extend(self._insert_batch(batch, op, ct))
        _elapsed = (time.time() - _t0) * 1000
        ids_preview = all_ids[:3] + (["...共%d条" % len(all_ids)] if len(all_ids) > 3 else [])
        self._log("新增记录(汇总)", _elapsed,
                  "总输入%d条, 分%d批, 成功%d条, IDs=%s" % (
                      len(records), len(batches), len(all_ids), ids_preview))
        return all_ids

    # -- 查：列出多行记录 --------------------------------------------------

    def list_records(
        self,
        operator_id: Optional[str] = None,
        max_results: int = 100,
        next_token: Optional[str] = None,
        field_id_or_names: Optional[List[str]] = None,
        filter_conditions: Optional[List[Dict[str, Any]]] = None,
        filter_combination: str = "and",
        calc_fields: bool = False,
    ) -> Dict[str, Any]:
        """
        列出多行记录（支持分页、筛选）。

        请求头自动携带 x-acs-dingtalk-access-token；
        URL 查询参数自动携带 operatorId。

        :param max_results: 每页数量，1-100，默认 100
        :param next_token: 分页游标，首次不传
        :param field_id_or_names: 只返回指定字段
        :param filter_conditions: [{"field": "字段名", "operator": ">", "value": [值]}, ...]
            operator 支持: >, >=, <, <=, =, ==, !=, <>, contain, not_contain,
            以及钉钉原生操作符 equal/notEqual/greaterThan/...（自动识别转换）。
            注意：操作符可用性取决于字段类型，text 字段通常仅支持 equal/notEqual，
            数字/日期字段支持比较操作符。
        :param filter_combination: "and" / "or"
        :param calc_fields: 是否返回计算字段、查找引用的值，默认 False
        :return: {"hasMore": bool, "nextToken": str|None, "records": [...]}
        """
        op = self._require_operator(operator_id)
        cli, token = self._client_and_token()
        req = notable_models.ListRecordsRequest(
            operator_id=op,              # → 查询参数 operatorId
            max_results=max_results,
            next_token=next_token,
            field_id_or_names=field_id_or_names,
            calc_fields=calc_fields,     # → body 参数 calcFields
        )
        if filter_conditions:
            conditions = []
            for c in filter_conditions:
                cond = dict(c)
                cond["operator"] = normalize_operator(cond.get("operator", "equal"))
                conditions.append(notable_models.ListRecordsRequestFilterConditions(**cond))
            req.filter = notable_models.ListRecordsRequestFilter(
                combination=filter_combination,
                conditions=conditions,
            )

        headers = notable_models.ListRecordsHeaders(
            x_acs_dingtalk_access_token=token,  # → 请求头
        )
        _t0 = time.time()
        resp = self._retry_call("查询记录", lambda: cli.list_records_with_options(
            self.base_id, self.sheet_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        body = resp.body
        records = []
        for r in (body.records or []):
            records.append({
                "id": r.id,
                "fields": r.fields,
                "createdTime": r.created_time,
                "lastModifiedTime": r.last_modified_time,
            })
        # 构建查询条件摘要
        _filter_desc = ""
        if filter_conditions:
            parts = []
            for c in filter_conditions:
                parts.append("%s%s%s" % (
                    c.get("field", "?"),
                    c.get("operator", "="),
                    c.get("value", []),
                ))
            _filter_desc = " 筛选=[%s]%s" % (filter_combination, " AND ".join(parts) if filter_combination == "and" else " OR ".join(parts))
        _calc = " calcFields" if calc_fields else ""
        self._log("查询记录", _elapsed,
                  "本页%d条 hasMore=%s%s%s" % (len(records), body.has_more, _filter_desc, _calc))
        return {
            "hasMore": body.has_more,
            "nextToken": body.next_token,
            "records": records,
        }

    def list_all_records(
        self,
        operator_id: Optional[str] = None,
        field_id_or_names: Optional[List[str]] = None,
        filter_conditions: Optional[List[Dict[str, Any]]] = None,
        filter_combination: str = "and",
        calc_fields: bool = False,
    ) -> List[Dict[str, Any]]:
        """自动翻页，返回全部记录。"""
        all_records: List[Dict[str, Any]] = []
        next_token = None
        _page_count = 0
        _t0 = time.time()
        while True:
            page = self.list_records(
                operator_id=operator_id,
                max_results=100,
                next_token=next_token,
                field_id_or_names=field_id_or_names,
                filter_conditions=filter_conditions,
                filter_combination=filter_combination,
                calc_fields=calc_fields,
            )
            _page_count += 1
            all_records.extend(page["records"])
            if not page["hasMore"]:
                break
            next_token = page["nextToken"]
        _elapsed = (time.time() - _t0) * 1000
        self._log("全量查询", _elapsed, "共%d页, 总计%d条" % (_page_count, len(all_records)))
        return all_records

    # -- 查：获取单行记录 --------------------------------------------------

    def get_record(
        self,
        record_id: str,
        operator_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        获取单行记录。

        请求头自动携带 x-acs-dingtalk-access-token；
        URL 查询参数自动携带 operatorId。

        :param record_id: 记录 ID
        :return: {"id": ..., "fields": {...}, "createdTime": ..., "lastModifiedTime": ...}
        """
        op = self._require_operator(operator_id)
        cli, token = self._client_and_token()
        req = notable_models.GetRecordRequest(operator_id=op)  # → 查询参数 operatorId
        headers = notable_models.GetRecordHeaders(
            x_acs_dingtalk_access_token=token,  # → 请求头
        )
        _t0 = time.time()
        resp = self._retry_call("获取单行", lambda: cli.get_record_with_options(
            self.base_id, self.sheet_id_or_name, record_id, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        body = resp.body
        self._log("获取单行", _elapsed, "recordId=%s, 字段数=%d" % (record_id, len(body.fields or {})))
        return {
            "id": body.id,
            "fields": body.fields,
            "createdTime": body.created_time,
            "lastModifiedTime": body.last_modified_time,
        }

    # -- 改：更新多行记录 --------------------------------------------------

    def _update_batch(
        self,
        records: List[Dict[str, Any]],
        operator_id: str,
    ) -> List[str]:
        """单批更新（不超过 UPDATE_BATCH_SIZE 条）。"""
        cli, token = self._client_and_token()
        req_records = [
            notable_models.UpdateRecordsRequestRecords(id=rec["id"], fields=rec["fields"])
            for rec in records
        ]
        req = notable_models.UpdateRecordsRequest(
            operator_id=operator_id,
            records=req_records,
        )
        headers = notable_models.UpdateRecordsHeaders(
            x_acs_dingtalk_access_token=token,
        )
        _t0 = time.time()
        resp = self._retry_call("更新记录", lambda: cli.update_records_with_options(
            self.base_id, self.sheet_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        ids = [item.id for item in (resp.body.value or [])]
        self._log("更新记录", _elapsed,
                  "本批输入%d条, 成功%d条" % (len(records), len(ids)))
        return ids

    def update_records(
        self,
        records: List[Dict[str, Any]],
        operator_id: Optional[str] = None,
    ) -> List[str]:
        """
        更新多行记录（自动分批，每批最多 %d 条）。

        请求头自动携带 x-acs-dingtalk-access-token；
        URL 查询参数自动携带 operatorId。

        :param records: [{"id": "记录ID", "fields": {"字段名": 新值}}, ...]
        :return: 更新成功的记录 ID 列表
        """ % UPDATE_BATCH_SIZE
        op = self._require_operator(operator_id)
        if not records:
            return []
        batches = _chunk_list(records, UPDATE_BATCH_SIZE)
        all_ids: List[str] = []
        _t0 = time.time()
        for batch in batches:
            all_ids.extend(self._update_batch(batch, op))
        _elapsed = (time.time() - _t0) * 1000
        ids_preview = all_ids[:3] + (["...共%d条" % len(all_ids)] if len(all_ids) > 3 else [])
        self._log("更新记录(汇总)", _elapsed,
                  "总输入%d条, 分%d批, 成功%d条, IDs=%s" % (
                      len(records), len(batches), len(all_ids), ids_preview))
        return all_ids

    # -- 删：删除多行记录 --------------------------------------------------

    def _delete_batch(
        self,
        record_ids: List[str],
        operator_id: str,
    ) -> bool:
        """单批删除（不超过 DELETE_BATCH_SIZE 条）。"""
        cli, token = self._client_and_token()
        req = notable_models.DeleteRecordsRequest(
            operator_id=operator_id,
            record_ids=record_ids,
        )
        headers = notable_models.DeleteRecordsHeaders(
            x_acs_dingtalk_access_token=token,
        )
        _t0 = time.time()
        resp = self._retry_call("删除记录", lambda: cli.delete_records_with_options(
            self.base_id, self.sheet_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        success = bool(resp.body.success)
        self._log("删除记录", _elapsed,
                  "本批删除%d条, 结果=%s" % (len(record_ids), success))
        return success

    def delete_records(
        self,
        record_ids: List[str],
        operator_id: Optional[str] = None,
    ) -> bool:
        """
        删除多行记录（自动分批，每批最多 %d 条）。

        请求头自动携带 x-acs-dingtalk-access-token；
        URL 查询参数自动携带 operatorId。

        :param record_ids: 要删除的记录 ID 列表
        :return: 是否全部成功
        """ % DELETE_BATCH_SIZE
        op = self._require_operator(operator_id)
        if not record_ids:
            return True
        batches = _chunk_list(record_ids, DELETE_BATCH_SIZE)
        _t0 = time.time()
        all_success = True
        for batch in batches:
            ok = self._delete_batch(batch, op)
            all_success = all_success and ok
        _elapsed = (time.time() - _t0) * 1000
        ids_preview = record_ids[:3] + (["...共%d条" % len(record_ids)] if len(record_ids) > 3 else [])
        self._log("删除记录(汇总)", _elapsed,
                  "总删除%d条, 分%d批, 结果=%s, IDs=%s" % (
                      len(record_ids), len(batches), all_success, ids_preview))
        return all_success

    # -- 查重新增：只新增不存在的数据 --------------------------------------

    def insert_records_if_not_exists(
        self,
        records: List[Dict[str, Any]],
        dedup_fields: Optional[List[str]] = None,
        operator_id: Optional[str] = None,
        client_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        查重后仅新增不存在的记录。

        先拉取目标表全部已有记录，按指定列（或全部列）构建指纹，
        已存在的记录跳过，只新增不存在的记录；同一批次内部的重复记录也会去重。

        请求头自动携带 x-acs-dingtalk-access-token；
        URL 查询参数自动携带 operatorId。

        :param records: 待新增记录 [{"字段名": 值, ...}, ...]
        :param dedup_fields: 查重字段（列）列表：
            - None（默认）：对比全部列，所有字段值都相同才判定为重复；
            - ["字段名"]：按单个字段查重（如按"员工id"）；
            - ["字段1", "字段2"]：多字段组合查重（这些字段值都相同才算重复）。
        :param operator_id: 操作人 unionId（可选，默认用初始化时的）
        :param client_token: 幂等键 UUID v4（可选，仅第一批生效）
        :return: {
            "inserted_ids": [...],   # 实际新增成功的记录ID
            "inserted_count": int,   # 新增条数
            "skipped_count": int,    # 判定为已存在而跳过的条数
            "skipped_indexes": [int] # 被跳过记录在输入 records 中的下标
          }
        """
        op = self._require_operator(operator_id)
        if not records:
            return {"inserted_ids": [], "inserted_count": 0,
                    "skipped_count": 0, "skipped_indexes": []}

        _t0 = time.time()
        # 1) 拉取全表已有记录，构建指纹集合
        existing = self.list_all_records(operator_id=op)
        existing_fps = set()
        for r in existing:
            existing_fps.add(_make_dedup_fingerprint(r.get("fields", {}), dedup_fields))

        # 2) 逐条比对，挑出不存在的记录（同时对批内重复去重）
        to_insert: List[Dict[str, Any]] = []
        skipped_indexes: List[int] = []
        for idx, rec in enumerate(records):
            fp = _make_dedup_fingerprint(rec, dedup_fields)
            if fp in existing_fps:
                skipped_indexes.append(idx)
                continue
            existing_fps.add(fp)  # 占位，避免同批次内重复新增
            to_insert.append(rec)

        # 3) 批量新增不存在的记录（insert_records 内部自动分批）
        inserted_ids = self.insert_records(
            to_insert, operator_id=op, client_token=client_token
        ) if to_insert else []

        _elapsed = (time.time() - _t0) * 1000
        _scope = "全部列" if not dedup_fields else "+".join(dedup_fields)
        self._log("查重新增(汇总)", _elapsed,
                  "查重字段=[%s], 输入%d条, 新增%d条, 跳过已存在%d条" % (
                      _scope, len(records), len(inserted_ids), len(skipped_indexes)))
        return {
            "inserted_ids": inserted_ids,
            "inserted_count": len(inserted_ids),
            "skipped_count": len(skipped_indexes),
            "skipped_indexes": skipped_indexes,
        }

    # ======================================================================
    # 异步方法（async/await，基于 SDK async 接口，非阻塞）
    # ======================================================================

    async def _client_and_token_async(self):
        """异步获取 AI 表客户端和当前有效的 access_token。"""
        token = await get_access_token_async(self.client_id, self.client_secret)
        return _build_notable_client(), token

    async def _insert_batch_async(
        self,
        records: List[Dict[str, Any]],
        operator_id: str,
        client_token: Optional[str] = None,
    ) -> List[str]:
        """异步单批新增（不超过 INSERT_BATCH_SIZE 条）。"""
        cli, token = await self._client_and_token_async()
        req_records = [
            notable_models.InsertRecordsRequestRecords(fields=rec)
            for rec in records
        ]
        req = notable_models.InsertRecordsRequest(
            operator_id=operator_id, records=req_records, client_token=client_token,
        )
        headers = notable_models.InsertRecordsHeaders(
            x_acs_dingtalk_access_token=token,
        )
        _t0 = time.time()
        resp = await self._retry_call_async("异步新增", lambda: cli.insert_records_with_options_async(
            self.base_id, self.sheet_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        ids = [item.id for item in (resp.body.value or [])]
        self._log("异步新增", _elapsed,
                  "本批输入%d条, 成功%d条" % (len(records), len(ids)))
        return ids

    async def insert_records_async(
        self,
        records: List[Dict[str, Any]],
        operator_id: Optional[str] = None,
        client_token: Optional[str] = None,
    ) -> List[str]:
        """异步新增多行记录（自动分批，每批最多 %d 条）。参数同 insert_records。""" % INSERT_BATCH_SIZE
        op = self._require_operator(operator_id)
        if not records:
            return []
        batches = _chunk_list(records, INSERT_BATCH_SIZE)
        all_ids: List[str] = []
        _t0 = time.time()
        for i, batch in enumerate(batches):
            ct = client_token if i == 0 else None
            all_ids.extend(await self._insert_batch_async(batch, op, ct))
        _elapsed = (time.time() - _t0) * 1000
        ids_preview = all_ids[:3] + (["...共%d条" % len(all_ids)] if len(all_ids) > 3 else [])
        self._log("异步新增(汇总)", _elapsed,
                  "总输入%d条, 分%d批, 成功%d条, IDs=%s" % (
                      len(records), len(batches), len(all_ids), ids_preview))
        return all_ids

    async def list_records_async(
        self,
        operator_id: Optional[str] = None,
        max_results: int = 100,
        next_token: Optional[str] = None,
        field_id_or_names: Optional[List[str]] = None,
        filter_conditions: Optional[List[Dict[str, Any]]] = None,
        filter_combination: str = "and",
        calc_fields: bool = False,
    ) -> Dict[str, Any]:
        """异步列出多行记录。参数同 list_records。"""
        op = self._require_operator(operator_id)
        cli, token = await self._client_and_token_async()
        req = notable_models.ListRecordsRequest(
            operator_id=op, max_results=max_results, next_token=next_token,
            field_id_or_names=field_id_or_names, calc_fields=calc_fields,
        )
        if filter_conditions:
            conditions = []
            for c in filter_conditions:
                cond = dict(c)
                cond["operator"] = normalize_operator(cond.get("operator", "equal"))
                conditions.append(notable_models.ListRecordsRequestFilterConditions(**cond))
            req.filter = notable_models.ListRecordsRequestFilter(
                combination=filter_combination, conditions=conditions,
            )
        headers = notable_models.ListRecordsHeaders(
            x_acs_dingtalk_access_token=token,
        )
        _t0 = time.time()
        resp = await self._retry_call_async("异步查询", lambda: cli.list_records_with_options_async(
            self.base_id, self.sheet_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        body = resp.body
        records = []
        for r in (body.records or []):
            records.append({
                "id": r.id, "fields": r.fields,
                "createdTime": r.created_time, "lastModifiedTime": r.last_modified_time,
            })
        _filter_desc = ""
        if filter_conditions:
            parts = ["%s%s%s" % (c.get("field","?"), c.get("operator","="), c.get("value",[])) for c in filter_conditions]
            _filter_desc = " 筛选=[%s]%s" % (filter_combination, " AND ".join(parts) if filter_combination == "and" else " OR ".join(parts))
        _calc = " calcFields" if calc_fields else ""
        self._log("异步查询", _elapsed,
                  "本页%d条 hasMore=%s%s%s" % (len(records), body.has_more, _filter_desc, _calc))
        return {"hasMore": body.has_more, "nextToken": body.next_token, "records": records}

    async def list_all_records_async(
        self,
        operator_id: Optional[str] = None,
        field_id_or_names: Optional[List[str]] = None,
        filter_conditions: Optional[List[Dict[str, Any]]] = None,
        filter_combination: str = "and",
        calc_fields: bool = False,
    ) -> List[Dict[str, Any]]:
        """异步自动翻页，返回全部记录。"""
        all_records: List[Dict[str, Any]] = []
        next_token = None
        _page_count = 0
        _t0 = time.time()
        while True:
            page = await self.list_records_async(
                operator_id=operator_id, max_results=100, next_token=next_token,
                field_id_or_names=field_id_or_names, filter_conditions=filter_conditions,
                filter_combination=filter_combination, calc_fields=calc_fields,
            )
            _page_count += 1
            all_records.extend(page["records"])
            if not page["hasMore"]:
                break
            next_token = page["nextToken"]
        _elapsed = (time.time() - _t0) * 1000
        self._log("异步全量查询", _elapsed, "共%d页, 总计%d条" % (_page_count, len(all_records)))
        return all_records

    async def get_record_async(
        self,
        record_id: str,
        operator_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """异步获取单行记录。参数同 get_record。"""
        op = self._require_operator(operator_id)
        cli, token = await self._client_and_token_async()
        req = notable_models.GetRecordRequest(operator_id=op)
        headers = notable_models.GetRecordHeaders(x_acs_dingtalk_access_token=token)
        _t0 = time.time()
        resp = await self._retry_call_async("异步获取单行", lambda: cli.get_record_with_options_async(
            self.base_id, self.sheet_id_or_name, record_id, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        body = resp.body
        self._log("异步获取单行", _elapsed, "recordId=%s, 字段数=%d" % (record_id, len(body.fields or {})))
        return {
            "id": body.id, "fields": body.fields,
            "createdTime": body.created_time, "lastModifiedTime": body.last_modified_time,
        }

    async def _update_batch_async(
        self,
        records: List[Dict[str, Any]],
        operator_id: str,
    ) -> List[str]:
        """异步单批更新（不超过 UPDATE_BATCH_SIZE 条）。"""
        cli, token = await self._client_and_token_async()
        req_records = [
            notable_models.UpdateRecordsRequestRecords(id=rec["id"], fields=rec["fields"])
            for rec in records
        ]
        req = notable_models.UpdateRecordsRequest(operator_id=operator_id, records=req_records)
        headers = notable_models.UpdateRecordsHeaders(x_acs_dingtalk_access_token=token)
        _t0 = time.time()
        resp = await self._retry_call_async("异步更新", lambda: cli.update_records_with_options_async(
            self.base_id, self.sheet_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        ids = [item.id for item in (resp.body.value or [])]
        self._log("异步更新", _elapsed,
                  "本批输入%d条, 成功%d条" % (len(records), len(ids)))
        return ids

    async def update_records_async(
        self,
        records: List[Dict[str, Any]],
        operator_id: Optional[str] = None,
    ) -> List[str]:
        """异步更新多行记录（自动分批，每批最多 %d 条）。参数同 update_records。""" % UPDATE_BATCH_SIZE
        op = self._require_operator(operator_id)
        if not records:
            return []
        batches = _chunk_list(records, UPDATE_BATCH_SIZE)
        all_ids: List[str] = []
        _t0 = time.time()
        for batch in batches:
            all_ids.extend(await self._update_batch_async(batch, op))
        _elapsed = (time.time() - _t0) * 1000
        ids_preview = all_ids[:3] + (["...共%d条" % len(all_ids)] if len(all_ids) > 3 else [])
        self._log("异步更新(汇总)", _elapsed,
                  "总输入%d条, 分%d批, 成功%d条, IDs=%s" % (
                      len(records), len(batches), len(all_ids), ids_preview))
        return all_ids

    async def _delete_batch_async(
        self,
        record_ids: List[str],
        operator_id: str,
    ) -> bool:
        """异步单批删除（不超过 DELETE_BATCH_SIZE 条）。"""
        cli, token = await self._client_and_token_async()
        req = notable_models.DeleteRecordsRequest(operator_id=operator_id, record_ids=record_ids)
        headers = notable_models.DeleteRecordsHeaders(x_acs_dingtalk_access_token=token)
        _t0 = time.time()
        resp = await self._retry_call_async("异步删除", lambda: cli.delete_records_with_options_async(
            self.base_id, self.sheet_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        success = bool(resp.body.success)
        self._log("异步删除", _elapsed,
                  "本批删除%d条, 结果=%s" % (len(record_ids), success))
        return success

    async def delete_records_async(
        self,
        record_ids: List[str],
        operator_id: Optional[str] = None,
    ) -> bool:
        """异步删除多行记录（自动分批，每批最多 %d 条）。参数同 delete_records。""" % DELETE_BATCH_SIZE
        op = self._require_operator(operator_id)
        if not record_ids:
            return True
        batches = _chunk_list(record_ids, DELETE_BATCH_SIZE)
        _t0 = time.time()
        all_success = True
        for batch in batches:
            ok = await self._delete_batch_async(batch, op)
            all_success = all_success and ok
        _elapsed = (time.time() - _t0) * 1000
        ids_preview = record_ids[:3] + (["...共%d条" % len(record_ids)] if len(record_ids) > 3 else [])
        self._log("异步删除(汇总)", _elapsed,
                  "总删除%d条, 分%d批, 结果=%s, IDs=%s" % (
                      len(record_ids), len(batches), all_success, ids_preview))
        return all_success

    async def insert_records_if_not_exists_async(
        self,
        records: List[Dict[str, Any]],
        dedup_fields: Optional[List[str]] = None,
        operator_id: Optional[str] = None,
        client_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """异步版「查重后仅新增」，参数与返回值同 insert_records_if_not_exists。"""
        op = self._require_operator(operator_id)
        if not records:
            return {"inserted_ids": [], "inserted_count": 0,
                    "skipped_count": 0, "skipped_indexes": []}

        _t0 = time.time()
        # 1) 拉取全表已有记录，构建指纹集合
        existing = await self.list_all_records_async(operator_id=op)
        existing_fps = set()
        for r in existing:
            existing_fps.add(_make_dedup_fingerprint(r.get("fields", {}), dedup_fields))

        # 2) 逐条比对，挑出不存在的记录（同时对批内重复去重）
        to_insert: List[Dict[str, Any]] = []
        skipped_indexes: List[int] = []
        for idx, rec in enumerate(records):
            fp = _make_dedup_fingerprint(rec, dedup_fields)
            if fp in existing_fps:
                skipped_indexes.append(idx)
                continue
            existing_fps.add(fp)
            to_insert.append(rec)

        # 3) 批量新增不存在的记录
        inserted_ids = await self.insert_records_async(
            to_insert, operator_id=op, client_token=client_token
        ) if to_insert else []

        _elapsed = (time.time() - _t0) * 1000
        _scope = "全部列" if not dedup_fields else "+".join(dedup_fields)
        self._log("异步查重新增(汇总)", _elapsed,
                  "查重字段=[%s], 输入%d条, 新增%d条, 跳过已存在%d条" % (
                      _scope, len(records), len(inserted_ids), len(skipped_indexes)))
        return {
            "inserted_ids": inserted_ids,
            "inserted_count": len(inserted_ids),
            "skipped_count": len(skipped_indexes),
            "skipped_indexes": skipped_indexes,
        }

    # ======================================================================
    # 字段管理（Field CRUD）
    # ======================================================================

    def create_field(
        self,
        name: str,
        field_type: str,
        property: Optional[Dict[str, Any]] = None,
        operator_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        创建字段。

        请求头自动携带 x-acs-dingtalk-access-token；
        URL 查询参数自动携带 operatorId。

        :param name: 字段名
        :param field_type: 字段类型，如 text / number / singleSelect / multiSelect / date / checkbox 等
        :param property: 字段属性字典（如单选/多选的 options），具体格式参考钉钉文档
        :param operator_id: 操作人 unionId（可选，默认用初始化时的）
        :return: {"id": ..., "name": ..., "type": ..., "property": ...}
        """
        op = self._require_operator(operator_id)
        cli, token = self._client_and_token()
        req = notable_models.CreateFieldRequest(
            operator_id=op,
            name=name,
            type=field_type,
            property=property,
        )
        headers = notable_models.CreateFieldHeaders(
            x_acs_dingtalk_access_token=token,
        )
        _t0 = time.time()
        resp = self._retry_call("创建字段", lambda: cli.create_field_with_options(
            self.base_id, self.sheet_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        body = resp.body
        self._log("创建字段", _elapsed,
                  "字段名=%s, 类型=%s, fieldId=%s" % (name, field_type, body.id))
        return {
            "id": body.id,
            "name": body.name,
            "type": body.type,
            "property": body.property,
        }

    def list_fields(self, operator_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取所有字段。

        请求头自动携带 x-acs-dingtalk-access-token；
        URL 查询参数自动携带 operatorId。

        :param operator_id: 操作人 unionId（可选，默认用初始化时的）
        :return: [{"id": ..., "name": ..., "type": ..., "property": ...}, ...]
        """
        op = self._require_operator(operator_id)
        cli, token = self._client_and_token()
        req = notable_models.GetAllFieldsRequest(operator_id=op)
        headers = notable_models.GetAllFieldsHeaders(
            x_acs_dingtalk_access_token=token,
        )
        _t0 = time.time()
        resp = self._retry_call("获取字段列表", lambda: cli.get_all_fields_with_options(
            self.base_id, self.sheet_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        fields = []
        for f in (resp.body.value or []):
            fields.append({
                "id": f.id,
                "name": f.name,
                "type": f.type,
                "property": f.property,
            })
        self._log("获取字段列表", _elapsed, "共%d个字段" % len(fields))
        return fields

    def update_field(
        self,
        field_id_or_name: str,
        name: Optional[str] = None,
        property: Optional[Dict[str, Any]] = None,
        operator_id: Optional[str] = None,
    ) -> str:
        """
        更新字段（仅支持修改字段名和属性，不支持修改字段类型）。

        请求头自动携带 x-acs-dingtalk-access-token；
        URL 查询参数自动携带 operatorId。

        :param field_id_or_name: 字段ID或字段名称
        :param name: 新的字段名（可选）
        :param property: 新的字段属性字典（可选）
        :param operator_id: 操作人 unionId（可选，默认用初始化时的）
        :return: 字段ID
        """
        op = self._require_operator(operator_id)
        cli, token = self._client_and_token()
        req = notable_models.UpdateFieldRequest(
            operator_id=op,
            name=name,
            property=property,
        )
        headers = notable_models.UpdateFieldHeaders(
            x_acs_dingtalk_access_token=token,
        )
        _t0 = time.time()
        resp = self._retry_call("更新字段", lambda: cli.update_field_with_options(
            self.base_id, self.sheet_id_or_name, field_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        self._log("更新字段", _elapsed,
                  "字段=%s, 新名称=%s" % (field_id_or_name, name or "(不变)"))
        return resp.body.id

    def delete_field(
        self,
        field_id_or_name: str,
        operator_id: Optional[str] = None,
    ) -> bool:
        """
        删除字段。

        请求头自动携带 x-acs-dingtalk-access-token；
        URL 查询参数自动携带 operatorId。

        注意：第一个字段（主字段）不可删除。

        :param field_id_or_name: 字段ID或字段名称
        :param operator_id: 操作人 unionId（可选，默认用初始化时的）
        :return: 是否成功
        """
        op = self._require_operator(operator_id)
        cli, token = self._client_and_token()
        req = notable_models.DeleteFieldRequest(operator_id=op)
        headers = notable_models.DeleteFieldHeaders(
            x_acs_dingtalk_access_token=token,
        )
        _t0 = time.time()
        resp = self._retry_call("删除字段", lambda: cli.delete_field_with_options(
            self.base_id, self.sheet_id_or_name, field_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        success = bool(resp.body.success) if hasattr(resp.body, 'success') else True
        self._log("删除字段", _elapsed,
                  "字段=%s, 结果=%s" % (field_id_or_name, success))
        return success

    # ======================================================================
    # 字段管理 —— 异步方法
    # ======================================================================

    async def create_field_async(
        self,
        name: str,
        field_type: str,
        property: Optional[Dict[str, Any]] = None,
        operator_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """异步创建字段。参数同 create_field。"""
        op = self._require_operator(operator_id)
        cli, token = await self._client_and_token_async()
        req = notable_models.CreateFieldRequest(
            operator_id=op, name=name, type=field_type, property=property,
        )
        headers = notable_models.CreateFieldHeaders(x_acs_dingtalk_access_token=token)
        _t0 = time.time()
        resp = await self._retry_call_async("异步创建字段", lambda: cli.create_field_with_options_async(
            self.base_id, self.sheet_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        body = resp.body
        self._log("异步创建字段", _elapsed,
                  "字段名=%s, 类型=%s, fieldId=%s" % (name, field_type, body.id))
        return {"id": body.id, "name": body.name, "type": body.type, "property": body.property}

    async def list_fields_async(self, operator_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """异步获取所有字段。参数同 list_fields。"""
        op = self._require_operator(operator_id)
        cli, token = await self._client_and_token_async()
        req = notable_models.GetAllFieldsRequest(operator_id=op)
        headers = notable_models.GetAllFieldsHeaders(x_acs_dingtalk_access_token=token)
        _t0 = time.time()
        resp = await self._retry_call_async("异步获取字段列表", lambda: cli.get_all_fields_with_options_async(
            self.base_id, self.sheet_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        fields = [{"id": f.id, "name": f.name, "type": f.type, "property": f.property}
                  for f in (resp.body.value or [])]
        self._log("异步获取字段列表", _elapsed, "共%d个字段" % len(fields))
        return fields

    async def update_field_async(
        self,
        field_id_or_name: str,
        name: Optional[str] = None,
        property: Optional[Dict[str, Any]] = None,
        operator_id: Optional[str] = None,
    ) -> str:
        """异步更新字段。参数同 update_field。"""
        op = self._require_operator(operator_id)
        cli, token = await self._client_and_token_async()
        req = notable_models.UpdateFieldRequest(operator_id=op, name=name, property=property)
        headers = notable_models.UpdateFieldHeaders(x_acs_dingtalk_access_token=token)
        _t0 = time.time()
        resp = await self._retry_call_async("异步更新字段", lambda: cli.update_field_with_options_async(
            self.base_id, self.sheet_id_or_name, field_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        self._log("异步更新字段", _elapsed,
                  "字段=%s, 新名称=%s" % (field_id_or_name, name or "(不变)"))
        return resp.body.id

    async def delete_field_async(
        self,
        field_id_or_name: str,
        operator_id: Optional[str] = None,
    ) -> bool:
        """异步删除字段。参数同 delete_field。"""
        op = self._require_operator(operator_id)
        cli, token = await self._client_and_token_async()
        req = notable_models.DeleteFieldRequest(operator_id=op)
        headers = notable_models.DeleteFieldHeaders(x_acs_dingtalk_access_token=token)
        _t0 = time.time()
        resp = await self._retry_call_async("异步删除字段", lambda: cli.delete_field_with_options_async(
            self.base_id, self.sheet_id_or_name, field_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        success = bool(resp.body.success) if hasattr(resp.body, 'success') else True
        self._log("异步删除字段", _elapsed,
                  "字段=%s, 结果=%s" % (field_id_or_name, success))
        return success

    # ======================================================================
    # 数据表管理（Sheet CRUD）
    # ======================================================================

    def create_sheet(
        self,
        name: str,
        fields: Optional[List[Dict[str, Any]]] = None,
        operator_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        创建数据表（在当前 AI 表格文档下新建一个数据表）。

        请求头自动携带 x-acs-dingtalk-access-token；
        URL 查询参数自动携带 operatorId。

        :param name: 数据表名称
        :param fields: 初始字段配置列表，如 [{"name": "标题", "type": "text"}, ...]
        :param operator_id: 操作人 unionId（可选，默认用初始化时的）
        :return: {"id": ..., "name": ...}
        """
        op = self._require_operator(operator_id)
        cli, token = self._client_and_token()
        req_fields = None
        if fields:
            req_fields = [
                notable_models.CreateSheetRequestFields(
                    name=f["name"], type=f["type"], property=f.get("property")
                )
                for f in fields
            ]
        req = notable_models.CreateSheetRequest(
            operator_id=op,
            name=name,
            fields=req_fields,
        )
        headers = notable_models.CreateSheetHeaders(
            x_acs_dingtalk_access_token=token,
        )
        _t0 = time.time()
        resp = self._retry_call("创建数据表", lambda: cli.create_sheet_with_options(
            self.base_id, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        body = resp.body
        self._log("创建数据表", _elapsed,
                  "表名=%s, sheetId=%s, 初始字段数=%d" % (name, body.id, len(fields or [])))
        return {"id": body.id, "name": body.name}

    def get_sheet(
        self,
        sheet_id_or_name: str,
        operator_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        获取单个数据表信息。

        :param sheet_id_or_name: 数据表 ID 或名称
        :param operator_id: 操作人 unionId（可选）
        :return: {"id": ..., "name": ...}
        """
        op = self._require_operator(operator_id)
        cli, token = self._client_and_token()
        req = notable_models.GetSheetRequest(operator_id=op)
        headers = notable_models.GetSheetHeaders(
            x_acs_dingtalk_access_token=token,
        )
        _t0 = time.time()
        resp = self._retry_call("获取数据表", lambda: cli.get_sheet_with_options(
            self.base_id, sheet_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        body = resp.body
        self._log("获取数据表", _elapsed,
                  "表=%s, sheetId=%s" % (sheet_id_or_name, body.id))
        return {"id": body.id, "name": body.name}

    def list_sheets(self, operator_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        获取当前 AI 表格文档下所有数据表。

        :param operator_id: 操作人 unionId（可选）
        :return: [{"id": ..., "name": ...}, ...]
        """
        op = self._require_operator(operator_id)
        cli, token = self._client_and_token()
        req = notable_models.GetAllSheetsRequest(operator_id=op)
        headers = notable_models.GetAllSheetsHeaders(
            x_acs_dingtalk_access_token=token,
        )
        _t0 = time.time()
        resp = self._retry_call("获取数据表列表", lambda: cli.get_all_sheets_with_options(
            self.base_id, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        sheets = [{"id": s.id, "name": s.name} for s in (resp.body.value or [])]
        self._log("获取数据表列表", _elapsed, "共%d个数据表" % len(sheets))
        return sheets

    def update_sheet(
        self,
        sheet_id_or_name: str,
        name: str,
        operator_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        更新数据表（重命名）。

        :param sheet_id_or_name: 数据表 ID 或名称
        :param name: 新的数据表名称
        :param operator_id: 操作人 unionId（可选）
        :return: {"id": ..., "name": ...}
        """
        op = self._require_operator(operator_id)
        cli, token = self._client_and_token()
        req = notable_models.UpdateSheetRequest(
            operator_id=op,
            name=name,
        )
        headers = notable_models.UpdateSheetHeaders(
            x_acs_dingtalk_access_token=token,
        )
        _t0 = time.time()
        resp = self._retry_call("更新数据表", lambda: cli.update_sheet_with_options(
            self.base_id, sheet_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        body = resp.body
        self._log("更新数据表", _elapsed,
                  "原表=%s, 新名称=%s, sheetId=%s" % (sheet_id_or_name, name, body.id))
        return {"id": body.id, "name": body.name}

    def delete_sheet(
        self,
        sheet_id_or_name: str,
        operator_id: Optional[str] = None,
    ) -> bool:
        """
        删除数据表。

        注意：删除后不可恢复，请谨慎操作。

        :param sheet_id_or_name: 数据表 ID 或名称
        :param operator_id: 操作人 unionId（可选）
        :return: 是否成功
        """
        op = self._require_operator(operator_id)
        cli, token = self._client_and_token()
        req = notable_models.DeleteSheetRequest(operator_id=op)
        headers = notable_models.DeleteSheetHeaders(
            x_acs_dingtalk_access_token=token,
        )
        _t0 = time.time()
        resp = self._retry_call("删除数据表", lambda: cli.delete_sheet_with_options(
            self.base_id, sheet_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        success = bool(resp.body.success) if hasattr(resp.body, 'success') else True
        self._log("删除数据表", _elapsed,
                  "表=%s, 结果=%s" % (sheet_id_or_name, success))
        return success

    # ======================================================================
    # 数据表管理 —— 异步方法
    # ======================================================================

    async def create_sheet_async(
        self,
        name: str,
        fields: Optional[List[Dict[str, Any]]] = None,
        operator_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """异步创建数据表。参数同 create_sheet。"""
        op = self._require_operator(operator_id)
        cli, token = await self._client_and_token_async()
        req_fields = None
        if fields:
            req_fields = [
                notable_models.CreateSheetRequestFields(
                    name=f["name"], type=f["type"], property=f.get("property")
                )
                for f in fields
            ]
        req = notable_models.CreateSheetRequest(operator_id=op, name=name, fields=req_fields)
        headers = notable_models.CreateSheetHeaders(x_acs_dingtalk_access_token=token)
        _t0 = time.time()
        resp = await self._retry_call_async("异步创建数据表", lambda: cli.create_sheet_with_options_async(
            self.base_id, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        body = resp.body
        self._log("异步创建数据表", _elapsed,
                  "表名=%s, sheetId=%s, 初始字段数=%d" % (name, body.id, len(fields or [])))
        return {"id": body.id, "name": body.name}

    async def get_sheet_async(
        self,
        sheet_id_or_name: str,
        operator_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """异步获取单个数据表。参数同 get_sheet。"""
        op = self._require_operator(operator_id)
        cli, token = await self._client_and_token_async()
        req = notable_models.GetSheetRequest(operator_id=op)
        headers = notable_models.GetSheetHeaders(x_acs_dingtalk_access_token=token)
        _t0 = time.time()
        resp = await self._retry_call_async("异步获取数据表", lambda: cli.get_sheet_with_options_async(
            self.base_id, sheet_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        body = resp.body
        self._log("异步获取数据表", _elapsed, "表=%s, sheetId=%s" % (sheet_id_or_name, body.id))
        return {"id": body.id, "name": body.name}

    async def list_sheets_async(self, operator_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """异步获取所有数据表。参数同 list_sheets。"""
        op = self._require_operator(operator_id)
        cli, token = await self._client_and_token_async()
        req = notable_models.GetAllSheetsRequest(operator_id=op)
        headers = notable_models.GetAllSheetsHeaders(x_acs_dingtalk_access_token=token)
        _t0 = time.time()
        resp = await self._retry_call_async("异步获取数据表列表", lambda: cli.get_all_sheets_with_options_async(
            self.base_id, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        sheets = [{"id": s.id, "name": s.name} for s in (resp.body.value or [])]
        self._log("异步获取数据表列表", _elapsed, "共%d个数据表" % len(sheets))
        return sheets

    async def update_sheet_async(
        self,
        sheet_id_or_name: str,
        name: str,
        operator_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        """异步更新数据表。参数同 update_sheet。"""
        op = self._require_operator(operator_id)
        cli, token = await self._client_and_token_async()
        req = notable_models.UpdateSheetRequest(operator_id=op, name=name)
        headers = notable_models.UpdateSheetHeaders(x_acs_dingtalk_access_token=token)
        _t0 = time.time()
        resp = await self._retry_call_async("异步更新数据表", lambda: cli.update_sheet_with_options_async(
            self.base_id, sheet_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        body = resp.body
        self._log("异步更新数据表", _elapsed,
                  "原表=%s, 新名称=%s, sheetId=%s" % (sheet_id_or_name, name, body.id))
        return {"id": body.id, "name": body.name}

    async def delete_sheet_async(
        self,
        sheet_id_or_name: str,
        operator_id: Optional[str] = None,
    ) -> bool:
        """异步删除数据表。参数同 delete_sheet。"""
        op = self._require_operator(operator_id)
        cli, token = await self._client_and_token_async()
        req = notable_models.DeleteSheetRequest(operator_id=op)
        headers = notable_models.DeleteSheetHeaders(x_acs_dingtalk_access_token=token)
        _t0 = time.time()
        resp = await self._retry_call_async("异步删除数据表", lambda: cli.delete_sheet_with_options_async(
            self.base_id, sheet_id_or_name, req, headers, self._runtime()
        ))
        _elapsed = (time.time() - _t0) * 1000
        success = bool(resp.body.success) if hasattr(resp.body, 'success') else True
        self._log("异步删除数据表", _elapsed, "表=%s, 结果=%s" % (sheet_id_or_name, success))
        return success
