# -*- coding: utf-8 -*-
"""
钉钉AI表格（Notable）服务端 API —— 增删查改基础封装

设计要点：
1. access_token 自动获取 + 缓存（7200s 有效期，提前60s续期），线程安全。
2. 每次请求：请求头自动携带 x-acs-dingtalk-access-token；URL 查询参数自动携带 operatorId。
3. 凭证可初始化时动态传入，也可从项目根目录 .config 读取；导入本模块时不产生任何网络请求。
4. 新增每批最多200条、更新/删除每批最多100条，超出自动拆分多批执行并合并结果。
5. 失败自动重试（默认10次、间隔5s），仅对 5xx / 429 / 网络错误重试，4xx 直接抛出。
6. 同时提供同步方法与 _async 异步方法（asyncio.to_thread 包装，可并发）。
"""

import os
import json
import time
import asyncio
import threading
import configparser
from typing import Any, Dict, List, Optional, Tuple

# 导入时不做版本强校验（避免仅读取配置时也强制依赖 SDK）；在真正实例化客户端时再校验。
from importlib.metadata import version as _pkg_version, PackageNotFoundError

from alibabacloud_tea_openapi import models as open_api_models
from alibabacloud_tea_util import models as util_models
from alibabacloud_dingtalk.oauth2_1_0 import client as oauth_client
from alibabacloud_dingtalk.oauth2_1_0 import models as oauth_models
from alibabacloud_dingtalk.notable_1_0 import client as notable_client
from alibabacloud_dingtalk.notable_1_0 import models as notable_models


# 要求的最低 SDK 版本（含新增 AI 表接口）
MIN_SDK_VERSION = "2.2.57"
# 钉钉 AI 表单批上限：新增200，更新/删除100
INSERT_BATCH_SIZE = 200
UPDATE_BATCH_SIZE = 100
DELETE_BATCH_SIZE = 100

# 条件筛选操作符别名 -> 钉钉官方操作符
OPERATOR_MAP = {
    "=": "equal",
    "==": "equal",
    "equal": "equal",
    "eq": "equal",
    "!=": "notEqual",
    "<>": "notEqual",
    "notEqual": "notEqual",
    "ne": "notEqual",
    ">": "greaterThan",
    "greaterThan": "greaterThan",
    "gt": "greaterThan",
    ">=": "greaterThanOrEqual",
    "greaterThanOrEqual": "greaterThanOrEqual",
    "gte": "greaterThanOrEqual",
    "<": "lessThan",
    "lessThan": "lessThan",
    "lt": "lessThan",
    "<=": "lessThanOrEqual",
    "lessThanOrEqual": "lessThanOrEqual",
    "lte": "lessThanOrEqual",
    "contain": "contains",
    "contains": "contains",
    "like": "contains",
    "not_contain": "notContains",
    "notContains": "notContains",
}


def load_config(config_path: Optional[str] = None) -> Dict[str, str]:
    """读取 .config（无 section 头的 ini 格式），返回 dict。"""
    if config_path is None:
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".config")
    parser = configparser.ConfigParser()
    with open(config_path, "r", encoding="utf-8") as f:
        # 给无 section 的配置补上 DEFAULT 段，便于 configparser 解析
        parser.read_string("[DEFAULT]\n" + f.read())
    return {k: v.strip() for k, v in parser["DEFAULT"].items()}


def _parse_version(v: str) -> Tuple[int, int, int]:
    parts = str(v).split(".")
    nums = []
    for p in parts[:3]:
        num = ""
        for ch in p:
            if ch.isdigit():
                num += ch
            else:
                break
        nums.append(int(num) if num else 0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)  # type: ignore


def _check_sdk_version() -> None:
    """校验 alibabacloud-dingtalk 版本 >= MIN_SDK_VERSION，否则抛 ImportError。"""
    try:
        installed = _pkg_version("alibabacloud-dingtalk")
    except PackageNotFoundError:
        raise ImportError(
            "未安装 alibabacloud-dingtalk，请先执行: pip install alibabacloud-dingtalk>=" + MIN_SDK_VERSION
        )
    if _parse_version(installed) < _parse_version(MIN_SDK_VERSION):
        raise ImportError(
            "alibabacloud-dingtalk 版本过低，当前 %s，需要 >= %s，请升级：pip install -U alibabacloud-dingtalk==%s"
            % (installed, MIN_SDK_VERSION, MIN_SDK_VERSION)
        )


# token 进程内缓存：{client_id: {"token": ..., "expire_at": ts}}
_token_cache: Dict[str, Dict[str, Any]] = {}
_token_lock = threading.Lock()


def get_access_token(client_id: str, client_secret: str, force_refresh: bool = False) -> str:
    """获取企业内部应用 access_token（带缓存，线程安全）。"""
    now = time.time()
    cached = _token_cache.get(client_id)
    if not force_refresh and cached and now < cached["expire_at"] - 60:
        return cached["token"]

    with _token_lock:
        # 双重检查，避免多线程同时刷新
        cached = _token_cache.get(client_id)
        now = time.time()
        if not force_refresh and cached and now < cached["expire_at"] - 60:
            return cached["token"]

        config = open_api_models.Config()
        config.protocol = "https"
        config.region_id = "central"
        client = oauth_client.Client(config)
        req = oauth_models.GetAccessTokenRequest(
            app_key=client_id,
            app_secret=client_secret,
        )
        resp = client.get_access_token(req)
        token = resp.body.access_token
        expire_in = resp.body.expire_in or 7200
        _token_cache[client_id] = {"token": token, "expire_at": time.time() + expire_in}
        return token


async def get_access_token_async(client_id: str, client_secret: str, force_refresh: bool = False) -> str:
    return await asyncio.to_thread(get_access_token, client_id, client_secret, force_refresh)


def _chunk_list(items: list, size: int) -> List[list]:
    """把列表按 size 切成多个小块。"""
    return [items[i:i + size] for i in range(0, len(items), size)]


def _is_retryable_error(exc: Exception) -> bool:
    """判断异常是否值得重试：5xx 服务端错误、429 限流、网络/超时类错误。"""
    status = getattr(getattr(exc, "data", None), "statusCode", None) or getattr(exc, "statusCode", None)
    if status is not None:
        try:
            code = int(status)
            if code == 429 or code >= 500:
                return True
            return False
        except (TypeError, ValueError):
            pass
    name = type(exc).__name__.lower()
    if any(k in name for k in ("timeout", "connection", "network", "ssl")):
        return True
    text = str(exc).lower()
    return any(k in text for k in ("timeout", "timed out", "connection", "network", "5xx", "internalerror", "serviceunavailable"))


# ---------------------------------------------------------------------------
# 查重新增相关的指纹/规范化辅助函数
# ---------------------------------------------------------------------------

def _normalize_dedup_value(value: Any) -> Any:
    """把钉钉读回值与本地写入值规范化为可比较的形式。

    处理：
    - 多选/单选等读回为 [{"name":..,"id":..}] 的对象数组 -> 排序后的名称列表；写入时是字符串数组
    - 数字读回可能是字符串 "10" -> 统一为数值形式
    - 空字符串 / 空数组 视为 None（钉钉读回时可能直接缺键）
    """
    if value is None:
        return None
    # 多选/人员/附件等对象数组：提取 name（无 name 时整体 JSON）
    if isinstance(value, list):
        names = []
        for it in value:
            if isinstance(it, dict):
                names.append(str(it.get("name", json.dumps(it, ensure_ascii=False, sort_keys=True))))
            else:
                names.append(_normalize_dedup_value(it))
        names = [n for n in names if n not in (None, "")]
        names.sort(key=lambda x: str(x))
        return names if names else None
    # 数字 / 数字字符串统一
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        s = value.strip()
        if s == "":
            return None
        # 数字字符串转数值（10.0 与 10 与 "10" 等价）
        try:
            f = float(s)
            return int(f) if f.is_integer() else f
        except ValueError:
            return s
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def _make_dedup_fingerprint(fields: Dict[str, Any], dedup_fields: Optional[List[str]] = None) -> str:
    """根据字段内容生成稳定指纹。dedup_fields 指定时只取这些列，否则取全部列。"""
    if dedup_fields:
        view = {k: _normalize_dedup_value(fields.get(k)) for k in dedup_fields}
    else:
        view = {k: _normalize_dedup_value(v) for k, v in fields.items()}
    return json.dumps(view, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class DingTalkAITable:
    """钉钉 AI 表格增删查改客户端（同步 + 异步）。"""

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
        # 实例化时强制校验 SDK 版本
        _check_sdk_version()

        # 判断是否全部凭证都由参数提供：若关键凭证任一缺失，则回退读取 .config 补全
        all_dynamic = all(v is not None for v in (client_id, client_secret, base_id, sheet_id_or_name))
        cfg = {}
        if not all_dynamic:
            try:
                cfg = load_config(config_path)
            except FileNotFoundError:
                cfg = {}

        self.client_id = client_id or cfg.get("client_id")
        self.client_secret = client_secret or cfg.get("client_secret")
        self.base_id = base_id or cfg.get("document_id")
        self.sheet_id_or_name = sheet_id_or_name or cfg.get("table_name")
        # operator_id 优先取参数，其次配置里的 operator_id / union_id
        self.operator_id = operator_id or cfg.get("operator_id") or cfg.get("union_id")

        self.max_retries = int(max_retries)
        self.retry_delay = float(retry_delay)

        missing = []
        if not self.client_id:
            missing.append("client_id")
        if not self.client_secret:
            missing.append("client_secret")
        if not self.base_id:
            missing.append("base_id(document_id)")
        if not self.sheet_id_or_name:
            missing.append("sheet_id_or_name(table_name)")
        if missing:
            raise ValueError("缺少必要凭证参数: %s，请动态传入或在 .config 中配置" % ", ".join(missing))

        self._client: Optional[notable_client.Client] = None
        self._client_lock = threading.Lock()

    # ---------- 内部基础设施 ----------

    def _get_client(self) -> notable_client.Client:
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    config = open_api_models.Config()
                    config.protocol = "https"
                    config.region_id = "central"
                    self._client = notable_client.Client(config)
        return self._client

    def _resolve_operator(self, operator_id: Optional[str]) -> str:
        op = operator_id or self.operator_id
        if not op:
            raise ValueError("缺少 operator_id（操作人 unionId），请在初始化或调用时传入")
        return op

    def _headers(self, token: str) -> util_models.RuntimeOptions:
        # 钉钉 SDK 把自定义请求头放在 RuntimeOptions 上
        rt = util_models.RuntimeOptions()
        rt.headers = {"x-acs-dingtalk-access-token": token}
        return rt

    def _request(self, label: str, fn, *args, **kwargs):
        """统一执行 + 自动重试。fn 为实际 SDK 调用（已绑定参数）。"""
        attempt = 0
        while True:
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                if attempt >= self.max_retries or not _is_retryable_error(exc):
                    raise
                wait = self.retry_delay
                print("[AI表] 重试 %s | 第%d/%d次 | 等待%.1fs | 错误: %s"
                      % (label, attempt + 1, self.max_retries, wait, str(exc)[:200]))
                time.sleep(wait)
                attempt += 1

    async def _request_async(self, label: str, fn, *args, **kwargs):
        attempt = 0
        while True:
            try:
                return await fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                if attempt >= self.max_retries or not _is_retryable_error(exc):
                    raise
                wait = self.retry_delay
                print("[AI表][async] 重试 %s | 第%d/%d次 | 等待%.1fs | 错误: %s"
                      % (label, attempt + 1, self.max_retries, wait, str(exc)[:200]))
                await asyncio.sleep(wait)
                attempt += 1

    @staticmethod
    def _record_to_dict(rec) -> Dict[str, Any]:
        def _val(v):
            if isinstance(v, list):
                return [_val(x) for x in v]
            if isinstance(v, dict):
                return {k: _val(x) for k, x in v.items()}
            # Tea model 对象转 dict
            if hasattr(v, "to_map"):
                try:
                    return _val(v.to_map())
                except Exception:  # noqa: BLE001
                    return v
            return v
        out = {
            "id": getattr(rec, "id", None),
            "fields": _val(getattr(rec, "fields", {})),
        }
        for attr in ("created_time", "last_modified_time", "createdTime", "lastModifiedTime"):
            if hasattr(rec, attr):
                out[attr] = getattr(rec, attr)
        return out

    def _build_filter(self, filter_conditions, combination: str = "and"):
        """把 [{field, operator, value}] 转成钉钉 ListRecordsFilter。

        operator 支持符号/别名：= != > >= < <= contain not_contain 及官方名。
        value 统一为列表。
        """
        if not filter_conditions:
            return None
        conds = []
        for c in filter_conditions:
            field = c.get("field") or c.get("field_id_or_name")
            op_raw = str(c.get("operator", "=")).strip()
            op = OPERATOR_MAP.get(op_raw)
            if op is None:
                # 直接传官方操作符时原样使用，未知操作符给出明确报错
                if op_raw in ("isEmpty", "isNotEmpty"):
                    op = op_raw
                else:
                    raise ValueError("不支持的筛选操作符: %s（支持 = != > >= < <= contain not_contain 等）" % op_raw)
            value = c.get("value")
            if value is None:
                value = []
            elif not isinstance(value, list):
                value = [value]
            conds.append(notable_models.ListRecordsFilterCondition(
                field=field, operator=op, value=value,
            ))
        return notable_models.ListRecordsFilter(
            combination="or" if str(combination).lower() == "or" else "and",
            conditions=conds,
        )

    # ======================================================================
    # 记录 CRUD
    # ======================================================================

    # ---------- 新增 ----------

    def insert_records(self, records: List[Dict[str, Any]], operator_id: Optional[str] = None,
                       client_token: Optional[str] = None) -> List[str]:
        """批量新增记录，自动按 200/批拆分，返回新增记录 ID 列表。"""
        if not records:
            return []
        op = self._resolve_operator(operator_id)
        client = self._get_client()
        token = get_access_token(self.client_id, self.client_secret)
        all_ids: List[str] = []
        total = len(records)
        for bi, batch in enumerate(_chunk_list(records, INSERT_BATCH_SIZE), 1):
            req_records = [notable_models.InsertRecordsRequestRecord(fields=r) for r in batch]
            req = notable_models.InsertRecordsRequest(
                operator_id=op,
                records=req_records,
                client_token=client_token,
            )
            t0 = time.time()
            resp = self._request(
                "新增记录", client.insert_records_with_options,
                self.base_id, self.sheet_id_or_name, req,
                self._headers(token), util_models.RuntimeOptions(),
            )
            ids = [r.id for r in (resp.body.value or [])]
            all_ids.extend(ids)
            print("[AI表] 新增 | 表:%s | 批次:%d | 本批:%d 累计:%d/%d | 耗时%.2fs | 返回%d条"
                  % (self.sheet_id_or_name, bi, len(batch), len(all_ids), total,
                     time.time() - t0, len(ids)))
        return all_ids

    async def insert_records_async(self, records, operator_id=None, client_token=None):
        return await asyncio.to_thread(self.insert_records, records, operator_id, client_token)

    def insert_records_if_not_exists(
        self,
        records: List[Dict[str, Any]],
        dedup_fields: Optional[List[str]] = None,
        operator_id: Optional[str] = None,
        client_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        """只新增目标表中尚不存在的记录。

        - dedup_fields 为空：对比全部字段，所有字段值都相同才算重复。
        - dedup_fields 指定：仅这些列的值都相同即视为重复。
        - 同一输入批次内部的重复记录也会被去重（只保留第一条）。
        返回 {inserted_ids, inserted_count, skipped_count, skipped_indexes}。
        """
        result = {"inserted_ids": [], "inserted_count": 0,
                  "skipped_count": 0, "skipped_indexes": []}
        if not records:
            return result

        op = self._resolve_operator(operator_id)
        t0 = time.time()
        existing = self.list_all_records(operator_id=op)
        existing_fp = set()
        for r in existing:
            existing_fp.add(_make_dedup_fingerprint(r.get("fields", {}), dedup_fields))
        print("[AI表] 查重新增 | 表:%s | 已有记录:%d | 查重列:%s | 拉取耗时%.2fs"
              % (self.sheet_id_or_name, len(existing),
                 "全部列" if not dedup_fields else ",".join(dedup_fields), time.time() - t0))

        to_insert = []
        seen = set(existing_fp)
        for idx, rec in enumerate(records):
            fp = _make_dedup_fingerprint(rec, dedup_fields)
            if fp in seen:
                result["skipped_indexes"].append(idx)
                continue
            seen.add(fp)
            to_insert.append(rec)

        result["skipped_count"] = len(result["skipped_indexes"])
        if to_insert:
            inserted_ids = self.insert_records(to_insert, operator_id=op, client_token=client_token)
            result["inserted_ids"] = inserted_ids
        result["inserted_count"] = len(result["inserted_ids"])
        print("[AI表] 查重新增完成 | 待新增:%d 实际新增:%d 跳过:%d"
              % (len(records), result["inserted_count"], result["skipped_count"]))
        return result

    async def insert_records_if_not_exists_async(self, records, dedup_fields=None,
                                                 operator_id=None, client_token=None):
        async def _work():
            return self.insert_records_if_not_exists(
                records, dedup_fields=dedup_fields,
                operator_id=operator_id, client_token=client_token)
        return await asyncio.to_thread(lambda: self.insert_records_if_not_exists(
            records, dedup_fields=dedup_fields, operator_id=operator_id, client_token=client_token))

    # ---------- 查询 ----------

    def list_records(self, operator_id: Optional[str] = None, max_results: int = 100,
                     next_token: Optional[str] = None, field_id_or_names: Optional[List[str]] = None,
                     filter_conditions: Optional[List[Dict]] = None,
                     filter_combination: str = "and",
                     calc_fields: bool = False) -> Dict[str, Any]:
        """列出（单页）记录。返回 {records, hasMore, nextToken, total}。"""
        op = self._resolve_operator(operator_id)
        client = self._get_client()
        token = get_access_token(self.client_id, self.client_secret)
        req = notable_models.ListRecordsRequest(
            operator_id=op,
            max_results=max_results,
            next_token=next_token,
            field_id_or_names=field_id_or_names,
        )
        flt = self._build_filter(filter_conditions, filter_combination)
        if flt is not None:
            req.filter = flt
        if calc_fields:
            req.calc_fields = True
        t0 = time.time()
        resp = self._request(
            "查询记录", client.list_records_with_options,
            self.base_id, self.sheet_id_or_name, req,
            self._headers(token), util_models.RuntimeOptions(),
        )
        body = resp.body
        records = [self._record_to_dict(r) for r in (body.records or [])]
        cond_desc = "无" if not filter_conditions else json.dumps(filter_conditions, ensure_ascii=False)
        print("[AI表] 查询 | 表:%s | 条件:%s | 本页:%d | hasMore:%s | 耗时%.2fs"
              % (self.sheet_id_or_name, cond_desc[:120], len(records),
                 bool(body.has_more), time.time() - t0))
        return {
            "records": records,
            "hasMore": bool(body.has_more),
            "nextToken": body.next_token,
            "total": getattr(body, "total", None),
        }

    async def list_records_async(self, **kwargs):
        return await asyncio.to_thread(lambda: self.list_records(**kwargs))

    def list_all_records(self, operator_id: Optional[str] = None, page_size: int = 100,
                         field_id_or_names=None, filter_conditions=None,
                         filter_combination: str = "and", calc_fields: bool = False,
                         max_results_limit: Optional[int] = None,
                         progress_every: int = 1000) -> List[Dict[str, Any]]:
        """自动翻页拉取全部记录。"""
        op = self._resolve_operator(operator_id)
        all_records: List[Dict[str, Any]] = []
        next_token = None
        page = 0
        t0 = time.time()
        while True:
            resp = self.list_records(
                operator_id=op, max_results=page_size, next_token=next_token,
                field_id_or_names=field_id_or_names, filter_conditions=filter_conditions,
                filter_combination=filter_combination, calc_fields=calc_fields,
            )
            all_records.extend(resp["records"])
            page += 1
            if page % max(1, progress_every // max(1, page_size)) == 0 and resp["hasMore"]:
                print("[AI表] 翻页拉取中 | 表:%s | 已累计:%d | 耗时%.2fs"
                      % (self.sheet_id_or_name, len(all_records), time.time() - t0))
            if max_results_limit is not None and len(all_records) >= max_results_limit:
                return all_records[:max_results_limit]
            if not resp["hasMore"] or not resp["nextToken"]:
                break
            next_token = resp["nextToken"]
        print("[AI表] 全量查询完成 | 表:%s | 总记录:%d | 页数:%d | 总耗时%.2fs"
              % (self.sheet_id_or_name, len(all_records), page, time.time() - t0))
        return all_records

    async def list_all_records_async(self, **kwargs):
        return await asyncio.to_thread(lambda: self.list_all_records(**kwargs))

    # ---------- 获取单行 ----------

    def get_record(self, record_id: str, operator_id: Optional[str] = None) -> Dict[str, Any]:
        op = self._resolve_operator(operator_id)
        client = self._get_client()
        token = get_access_token(self.client_id, self.client_secret)
        req = notable_models.GetRecordRequest(operator_id=op)
        t0 = time.time()
        resp = self._request(
            "获取单行", client.get_record_with_options,
            self.base_id, self.sheet_id_or_name, record_id, req,
            self._headers(token), util_models.RuntimeOptions(),
        )
        rec = self._record_to_dict(resp.body)
        print("[AI表] 获取单行 | 表:%s | id:%s | 耗时%.2fs"
              % (self.sheet_id_or_name, record_id, time.time() - t0))
        return rec

    async def get_record_async(self, record_id, operator_id=None):
        return await asyncio.to_thread(self.get_record, record_id, operator_id)

    # ---------- 更新 ----------

    def update_records(self, records: List[Dict[str, Any]], operator_id: Optional[str] = None) -> List[str]:
        """批量更新，records: [{"id":..., "fields":{...}}]，自动按 100/批拆分。"""
        if not records:
            return []
        op = self._resolve_operator(operator_id)
        client = self._get_client()
        token = get_access_token(self.client_id, self.client_secret)
        all_ids: List[str] = []
        total = len(records)
        for bi, batch in enumerate(_chunk_list(records, UPDATE_BATCH_SIZE), 1):
            req_records = [
                notable_models.UpdateRecordsRequestRecord(id=r["id"], fields=r["fields"])
                for r in batch
            ]
            req = notable_models.UpdateRecordsRequest(operator_id=op, records=req_records)
            t0 = time.time()
            resp = self._request(
                "更新记录", client.update_records_with_options,
                self.base_id, self.sheet_id_or_name, req,
                self._headers(token), util_models.RuntimeOptions(),
            )
            ids = [r.id for r in (resp.body.value or [])]
            all_ids.extend(ids)
            print("[AI表] 更新 | 表:%s | 批次:%d | 本批:%d 累计:%d/%d | 耗时%.2fs"
                  % (self.sheet_id_or_name, bi, len(batch), len(all_ids), total, time.time() - t0))
        return all_ids

    async def update_records_async(self, records, operator_id=None):
        return await asyncio.to_thread(self.update_records, records, operator_id)

    # ---------- 删除 ----------

    def delete_records(self, record_ids: List[str], operator_id: Optional[str] = None) -> bool:
        """批量删除，自动按 100/批拆分，全部成功返回 True。"""
        if not record_ids:
            return True
        op = self._resolve_operator(operator_id)
        client = self._get_client()
        token = get_access_token(self.client_id, self.client_secret)
        ok = True
        total = len(record_ids)
        done = 0
        for bi, batch in enumerate(_chunk_list(record_ids, DELETE_BATCH_SIZE), 1):
            req = notable_models.DeleteRecordsRequest(operator_id=op, record_ids=batch)
            t0 = time.time()
            resp = self._request(
                "删除记录", client.delete_records_with_options,
                self.base_id, self.sheet_id_or_name, req,
                self._headers(token), util_models.RuntimeOptions(),
            )
            batch_ok = bool(getattr(resp.body, "success", True))
            ok = ok and batch_ok
            done += len(batch)
            print("[AI表] 删除 | 表:%s | 批次:%d | 本批:%d 累计:%d/%d | 成功:%s | 耗时%.2fs"
                  % (self.sheet_id_or_name, bi, len(batch), done, total, batch_ok, time.time() - t0))
        return ok

    async def delete_records_async(self, record_ids, operator_id=None):
        return await asyncio.to_thread(self.delete_records, record_ids, operator_id)

    # ======================================================================
    # 字段（列）CRUD
    # ======================================================================

    def create_field(self, name: str, field_type: str, property: Optional[Dict] = None,
                     operator_id: Optional[str] = None) -> Dict[str, Any]:
        op = self._resolve_operator(operator_id)
        client = self._get_client()
        token = get_access_token(self.client_id, self.client_secret)
        req = notable_models.CreateFieldRequest(
            operator_id=op, name=name, type=field_type, property=property,
        )
        t0 = time.time()
        resp = self._request(
            "创建字段", client.create_field_with_options,
            self.base_id, self.sheet_id_or_name, req,
            self._headers(token), util_models.RuntimeOptions(),
        )
        b = resp.body
        print("[AI表] 创建字段 | 表:%s | 字段:%s(%s) | 耗时%.2fs"
              % (self.sheet_id_or_name, name, field_type, time.time() - t0))
        return {"id": b.id, "name": getattr(b, "name", name),
                "type": getattr(b, "type", field_type), "property": getattr(b, "property", None)}

    async def create_field_async(self, name, field_type, property=None, operator_id=None):
        return await asyncio.to_thread(self.create_field, name, field_type, property, operator_id)

    def list_fields(self, operator_id: Optional[str] = None) -> List[Dict[str, Any]]:
        op = self._resolve_operator(operator_id)
        client = self._get_client()
        token = get_access_token(self.client_id, self.client_secret)
        req = notable_models.GetAllFieldsRequest(operator_id=op)
        t0 = time.time()
        resp = self._request(
            "获取字段列表", client.get_all_fields_with_options,
            self.base_id, self.sheet_id_or_name, req,
            self._headers(token), util_models.RuntimeOptions(),
        )
        fields = []
        for f in (resp.body.value or []):
            fields.append({
                "id": getattr(f, "id", None),
                "name": getattr(f, "name", None),
                "type": getattr(f, "type", None),
                "property": getattr(f, "property", None),
            })
        print("[AI表] 字段列表 | 表:%s | 字段数:%d | 耗时%.2fs"
              % (self.sheet_id_or_name, len(fields), time.time() - t0))
        return fields

    async def list_fields_async(self, operator_id=None):
        return await asyncio.to_thread(self.list_fields, operator_id)

    def _resolve_field_id(self, field_id_or_name: str, operator_id: str) -> str:
        if field_id_or_name and not str(field_id_or_name).startswith("fld"):
            for f in self.list_fields(operator_id=operator_id):
                if f.get("name") == field_id_or_name:
                    return f["id"]
        return field_id_or_name

    def update_field(self, field_id_or_name: str, name: Optional[str] = None,
                     property: Optional[Dict] = None, operator_id: Optional[str] = None) -> str:
        op = self._resolve_operator(operator_id)
        field_id = self._resolve_field_id(field_id_or_name, op)
        client = self._get_client()
        token = get_access_token(self.client_id, self.client_secret)
        req = notable_models.UpdateFieldRequest(operator_id=op, name=name, property=property)
        t0 = time.time()
        resp = self._request(
            "更新字段", client.update_field_with_options,
            self.base_id, self.sheet_id_or_name, field_id, req,
            self._headers(token), util_models.RuntimeOptions(),
        )
        print("[AI表] 更新字段 | 表:%s | %s->%s | 耗时%.2fs"
              % (self.sheet_id_or_name, field_id_or_name, name, time.time() - t0))
        return getattr(resp.body, "id", field_id)

    async def update_field_async(self, field_id_or_name, name=None, property=None, operator_id=None):
        return await asyncio.to_thread(
            self.update_field, field_id_or_name, name, property, operator_id)

    def delete_field(self, field_id_or_name: str, operator_id: Optional[str] = None) -> bool:
        op = self._resolve_operator(operator_id)
        field_id = self._resolve_field_id(field_id_or_name, op)
        client = self._get_client()
        token = get_access_token(self.client_id, self.client_secret)
        req = notable_models.DeleteFieldRequest(operator_id=op)
        t0 = time.time()
        resp = self._request(
            "删除字段", client.delete_field_with_options,
            self.base_id, self.sheet_id_or_name, field_id, req,
            self._headers(token), util_models.RuntimeOptions(),
        )
        print("[AI表] 删除字段 | 表:%s | 字段:%s | 耗时%.2fs"
              % (self.sheet_id_or_name, field_id_or_name, time.time() - t0))
        return bool(getattr(resp.body, "success", True))

    async def delete_field_async(self, field_id_or_name, operator_id=None):
        return await asyncio.to_thread(self.delete_field, field_id_or_name, operator_id)

    # ======================================================================
    # 数据表（Sheet）CRUD
    # ======================================================================

    def create_sheet(self, name: str, fields: Optional[List[Dict]] = None,
                     operator_id: Optional[str] = None) -> Dict[str, Any]:
        op = self._resolve_operator(operator_id)
        client = self._get_client()
        token = get_access_token(self.client_id, self.client_secret)
        req_fields = None
        if fields:
            req_fields = [
                notable_models.CreateSheetRequestFields(
                    name=f["name"], type=f["type"], property=f.get("property"))
                for f in fields
            ]
        req = notable_models.CreateSheetRequest(operator_id=op, name=name, fields=req_fields)
        t0 = time.time()
        resp = self._request(
            "创建数据表", client.create_sheet_with_options,
            self.base_id, req, self._headers(token), util_models.RuntimeOptions(),
        )
        b = resp.body
        print("[AI表] 创建数据表 | Base:%s | 名称:%s | 耗时%.2fs"
              % (self.base_id, name, time.time() - t0))
        return {"id": b.id, "name": getattr(b, "name", name)}

    async def create_sheet_async(self, name, fields=None, operator_id=None):
        return await asyncio.to_thread(self.create_sheet, name, fields, operator_id)

    def _resolve_sheet_id(self, sheet_id_or_name: str, operator_id: str) -> str:
        if sheet_id_or_name and not str(sheet_id_or_name).startswith("st"):
            for s in self.list_sheets(operator_id=operator_id):
                if s.get("name") == sheet_id_or_name:
                    return s["id"]
        return sheet_id_or_name

    def get_sheet(self, sheet_id_or_name: str, operator_id: Optional[str] = None) -> Dict[str, Any]:
        op = self._resolve_operator(operator_id)
        sheet_id = self._resolve_sheet_id(sheet_id_or_name, op)
        client = self._get_client()
        token = get_access_token(self.client_id, self.client_secret)
        req = notable_models.GetSheetRequest(operator_id=op)
        t0 = time.time()
        resp = self._request(
            "获取数据表", client.get_sheet_with_options,
            self.base_id, sheet_id, req, self._headers(token), util_models.RuntimeOptions(),
        )
        b = resp.body
        print("[AI表] 获取数据表 | %s | 耗时%.2fs" % (sheet_id_or_name, time.time() - t0))
        return {"id": b.id, "name": getattr(b, "name", None)}

    async def get_sheet_async(self, sheet_id_or_name, operator_id=None):
        return await asyncio.to_thread(self.get_sheet, sheet_id_or_name, operator_id)

    def list_sheets(self, operator_id: Optional[str] = None) -> List[Dict[str, Any]]:
        op = self._resolve_operator(operator_id)
        client = self._get_client()
        token = get_access_token(self.client_id, self.client_secret)
        req = notable_models.GetAllSheetsRequest(operator_id=op)
        t0 = time.time()
        resp = self._request(
            "获取数据表列表", client.get_all_sheets_with_options,
            self.base_id, req, self._headers(token), util_models.RuntimeOptions(),
        )
        sheets = []
        for s in (resp.body.value or []):
            sheets.append({"id": getattr(s, "id", None), "name": getattr(s, "name", None)})
        print("[AI表] 数据表列表 | Base:%s | 数量:%d | 耗时%.2fs"
              % (self.base_id, len(sheets), time.time() - t0))
        return sheets

    async def list_sheets_async(self, operator_id=None):
        return await asyncio.to_thread(self.list_sheets, operator_id)

    def update_sheet(self, sheet_id_or_name: str, name: str,
                     operator_id: Optional[str] = None) -> Dict[str, Any]:
        op = self._resolve_operator(operator_id)
        sheet_id = self._resolve_sheet_id(sheet_id_or_name, op)
        client = self._get_client()
        token = get_access_token(self.client_id, self.client_secret)
        req = notable_models.UpdateSheetRequest(operator_id=op, name=name)
        t0 = time.time()
        resp = self._request(
            "更新数据表", client.update_sheet_with_options,
            self.base_id, sheet_id, req, self._headers(token), util_models.RuntimeOptions(),
        )
        b = resp.body
        print("[AI表] 更新数据表 | %s->%s | 耗时%.2fs"
              % (sheet_id_or_name, name, time.time() - t0))
        return {"id": getattr(b, "id", sheet_id), "name": getattr(b, "name", name)}

    async def update_sheet_async(self, sheet_id_or_name, name, operator_id=None):
        return await asyncio.to_thread(self.update_sheet, sheet_id_or_name, name, operator_id)

    def delete_sheet(self, sheet_id_or_name: str, operator_id: Optional[str] = None) -> bool:
        op = self._resolve_operator(operator_id)
        sheet_id = self._resolve_sheet_id(sheet_id_or_name, op)
        client = self._get_client()
        token = get_access_token(self.client_id, self.client_secret)
        req = notable_models.DeleteSheetRequest(operator_id=op)
        t0 = time.time()
        resp = self._request(
            "删除数据表", client.delete_sheet_with_options,
            self.base_id, sheet_id, req, self._headers(token), util_models.RuntimeOptions(),
        )
        print("[AI表] 删除数据表 | %s | 耗时%.2fs" % (sheet_id_or_name, time.time() - t0))
        return bool(getattr(resp.body, "success", True))

    async def delete_sheet_async(self, sheet_id_or_name, operator_id=None):
        return await asyncio.to_thread(self.delete_sheet, sheet_id_or_name, operator_id)
