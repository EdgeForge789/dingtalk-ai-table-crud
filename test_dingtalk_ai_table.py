# -*- coding: utf-8 -*-
"""
钉钉AI表格 CRUD 模块测试用例

运行方式：
    python -m pytest test_dingtalk_ai_table.py -v

说明：
- 全部用例通过 mock 隔离钉钉 SDK，不依赖真实网络和凭证
- 如需真实联调，设置环境变量 RUN_INTEGRATION=1 并配置好 .config + operator_id
"""

import os
import sys
import time
import asyncio
import unittest
from unittest.mock import patch, MagicMock, PropertyMock, AsyncMock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import dingtalk_ai_table as m
from dingtalk_ai_table import DingTalkAITable


# ---------------------------------------------------------------------------
# 辅助：构造 mock 响应体
# ---------------------------------------------------------------------------

def _make_body(**kwargs):
    body = MagicMock()
    for k, v in kwargs.items():
        setattr(body, k, v)
    return body


def _make_response(body):
    resp = MagicMock()
    resp.body = body
    return resp


# ---------------------------------------------------------------------------
# 1. 配置加载测试
# ---------------------------------------------------------------------------

class TestLoadConfig(unittest.TestCase):

    def test_load_default_config(self):
        """默认路径加载 .config，关键字段非空"""
        cfg = m.load_config()
        self.assertIn("client_id", cfg)
        self.assertIn("client_secret", cfg)
        self.assertIn("document_id", cfg)
        self.assertIn("table_name", cfg)
        self.assertTrue(cfg["client_id"])
        self.assertTrue(cfg["client_secret"])

    def test_load_custom_path(self):
        """指定路径加载配置"""
        path = os.path.join(os.path.dirname(__file__), ".config")
        cfg = m.load_config(path)
        self.assertEqual(cfg["document_id"], cfg.get("document_id"))

    def test_config_not_prints_secrets(self):
        """配置加载后不应在异常信息中泄露完整 secret"""
        cfg = m.load_config()
        # 仅验证长度合理，不打印值
        self.assertGreater(len(cfg["client_secret"]), 10)


# ---------------------------------------------------------------------------
# 2. AccessToken 缓存测试
# ---------------------------------------------------------------------------

class TestAccessToken(unittest.TestCase):

    def setUp(self):
        # 每个用例前清空缓存
        m._token_cache.clear()

    @patch("dingtalk_ai_table.oauth_client.Client")
    def test_get_access_token_success(self, MockClient):
        """首次获取：调用 SDK，返回 token 并缓存"""
        mock_cli = MockClient.return_value
        mock_cli.get_access_token.return_value = _make_response(
            _make_body(access_token="tok_abc123", expire_in=7200)
        )
        token = m.get_access_token("cid", "csecret")
        self.assertEqual(token, "tok_abc123")
        mock_cli.get_access_token.assert_called_once()

    @patch("dingtalk_ai_table.oauth_client.Client")
    def test_access_token_cache_hit(self, MockClient):
        """缓存未过期：不重复调用 SDK"""
        mock_cli = MockClient.return_value
        mock_cli.get_access_token.return_value = _make_response(
            _make_body(access_token="tok_cached", expire_in=7200)
        )
        t1 = m.get_access_token("cid", "csecret")
        t2 = m.get_access_token("cid", "csecret")
        self.assertEqual(t1, t2)
        self.assertEqual(mock_cli.get_access_token.call_count, 1)

    @patch("dingtalk_ai_table.oauth_client.Client")
    def test_access_token_force_refresh(self, MockClient):
        """force_refresh=True：强制重新调用"""
        mock_cli = MockClient.return_value
        mock_cli.get_access_token.side_effect = [
            _make_response(_make_body(access_token="old", expire_in=7200)),
            _make_response(_make_body(access_token="new", expire_in=7200)),
        ]
        m.get_access_token("cid", "csecret")
        token = m.get_access_token("cid", "csecret", force_refresh=True)
        self.assertEqual(token, "new")
        self.assertEqual(mock_cli.get_access_token.call_count, 2)

    @patch("dingtalk_ai_table.oauth_client.Client")
    def test_access_token_expired_refresh(self, MockClient):
        """缓存过期：自动重新获取"""
        mock_cli = MockClient.return_value
        mock_cli.get_access_token.side_effect = [
            _make_response(_make_body(access_token="old", expire_in=7200)),
            _make_response(_make_body(access_token="refreshed", expire_in=7200)),
        ]
        m.get_access_token("cid", "csecret")
        # 手动把该 client_id 的过期时间设为过去
        m._token_cache["cid"]["expire_at"] = time.time() - 100
        token = m.get_access_token("cid", "csecret")
        self.assertEqual(token, "refreshed")

    @patch("dingtalk_ai_table.oauth_client.Client")
    def test_access_token_multi_client_isolation(self, MockClient):
        """不同 client_id 的 token 缓存互相隔离"""
        mock_cli = MockClient.return_value
        mock_cli.get_access_token.side_effect = [
            _make_response(_make_body(access_token="tok_a", expire_in=7200)),
            _make_response(_make_body(access_token="tok_b", expire_in=7200)),
        ]
        ta = m.get_access_token("cid_a", "cs_a")
        tb = m.get_access_token("cid_b", "cs_b")
        self.assertEqual(ta, "tok_a")
        self.assertEqual(tb, "tok_b")
        self.assertEqual(mock_cli.get_access_token.call_count, 2)
        # 再次获取各自的，都命中缓存
        m.get_access_token("cid_a", "cs_a")
        m.get_access_token("cid_b", "cs_b")
        self.assertEqual(mock_cli.get_access_token.call_count, 2)


# ---------------------------------------------------------------------------
# 3. DingTalkAITable 初始化测试
# ---------------------------------------------------------------------------

class TestInit(unittest.TestCase):

    def test_init_loads_config(self):
        """初始化自动加载配置"""
        api = DingTalkAITable(operator_id="op_001")
        self.assertTrue(api.base_id)
        self.assertTrue(api.sheet_id_or_name)
        self.assertEqual(api.operator_id, "op_001")

    def test_init_operator_from_config(self):
        """operator_id 可从配置文件读取（若配置中有）"""
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
            "operator_id": "op_from_cfg",
        }):
            api = DingTalkAITable()
            self.assertEqual(api.operator_id, "op_from_cfg")

    def test_init_operator_param_overrides_config(self):
        """显式传入 operator_id 优先于配置"""
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
            "operator_id": "op_from_cfg",
        }):
            api = DingTalkAITable(operator_id="op_param")
            self.assertEqual(api.operator_id, "op_param")

    def test_init_union_id_fallback(self):
        """配置中无 operator_id 时回退到 union_id"""
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
            "union_id": "op_union",
        }):
            api = DingTalkAITable()
            self.assertEqual(api.operator_id, "op_union")

    def test_init_all_params_dynamic(self):
        """全部凭证动态传入，不读配置文件"""
        with patch.object(m, "load_config") as mock_load:
            api = DingTalkAITable(
                client_id="dyn_cid",
                client_secret="dyn_cs",
                base_id="dyn_base",
                sheet_id_or_name="dyn_sheet",
                operator_id="dyn_op",
            )
            mock_load.assert_not_called()  # 全参数传入时不读配置
        self.assertEqual(api.client_id, "dyn_cid")
        self.assertEqual(api.client_secret, "dyn_cs")
        self.assertEqual(api.base_id, "dyn_base")
        self.assertEqual(api.sheet_id_or_name, "dyn_sheet")
        self.assertEqual(api.operator_id, "dyn_op")

    def test_init_partial_params_fallback_to_config(self):
        """部分参数动态传入，其余从配置补全"""
        with patch.object(m, "load_config", return_value={
            "client_id": "cfg_cid", "client_secret": "cfg_cs",
            "document_id": "cfg_base", "table_name": "cfg_sheet",
            "union_id": "cfg_op",
        }):
            api = DingTalkAITable(client_id="dyn_cid", base_id="dyn_base")
        self.assertEqual(api.client_id, "dyn_cid")       # 动态传入优先
        self.assertEqual(api.base_id, "dyn_base")         # 动态传入优先
        self.assertEqual(api.client_secret, "cfg_cs")     # 从配置补全
        self.assertEqual(api.sheet_id_or_name, "cfg_sheet")
        self.assertEqual(api.operator_id, "cfg_op")

    def test_init_missing_required_raises(self):
        """缺少必填凭证时抛 ValueError 并列出缺失项"""
        with patch.object(m, "load_config", return_value={
            "client_id": "cid",  # 缺少 client_secret, document_id, table_name
        }):
            with self.assertRaises(ValueError) as ctx:
                DingTalkAITable()
        msg = str(ctx.exception)
        self.assertIn("client_secret", msg)
        self.assertIn("base_id", msg)
        self.assertIn("sheet_id_or_name", msg)

    def test_init_no_config_file_all_params_ok(self):
        """无配置文件但全部参数动态传入时正常"""
        with patch.object(m, "load_config", side_effect=FileNotFoundError):
            api = DingTalkAITable(
                client_id="cid", client_secret="cs",
                base_id="b", sheet_id_or_name="s", operator_id="op",
            )
        self.assertEqual(api.client_id, "cid")


# ---------------------------------------------------------------------------
# 4. 新增记录测试
# ---------------------------------------------------------------------------

class TestInsertRecords(unittest.TestCase):

    def _make_api(self):
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
        }):
            return DingTalkAITable(operator_id="op_001")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_insert_single_record(self, MockNotable, _mock_token):
        """新增单条记录"""
        mock_cli = MockNotable.return_value
        mock_cli.insert_records_with_options.return_value = _make_response(
            _make_body(value=[MagicMock(id="rec_001")])
        )
        api = self._make_api()
        ids = api.insert_records([{"标题": "hello", "数字": 1}])
        self.assertEqual(ids, ["rec_001"])
        mock_cli.insert_records_with_options.assert_called_once()
        # 校验路径参数
        call_args = mock_cli.insert_records_with_options.call_args
        self.assertEqual(call_args[0][0], "base1")
        self.assertEqual(call_args[0][1], "sheet1")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_insert_multiple_records(self, MockNotable, _mock_token):
        """新增多条记录"""
        mock_cli = MockNotable.return_value
        mock_cli.insert_records_with_options.return_value = _make_response(
            _make_body(value=[MagicMock(id="rec_1"), MagicMock(id="rec_2")])
        )
        api = self._make_api()
        ids = api.insert_records([{"a": 1}, {"a": 2}])
        self.assertEqual(ids, ["rec_1", "rec_2"])

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_insert_with_client_token(self, MockNotable, _mock_token):
        """新增时传入幂等键 client_token"""
        mock_cli = MockNotable.return_value
        mock_cli.insert_records_with_options.return_value = _make_response(_make_body(value=[]))
        api = self._make_api()
        api.insert_records([{"x": 1}], client_token="uuid-1234")
        req = mock_cli.insert_records_with_options.call_args[0][2]
        self.assertEqual(req.client_token, "uuid-1234")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_insert_operator_override(self, MockNotable, _mock_token):
        """调用时传入 operator_id 覆盖初始化值"""
        mock_cli = MockNotable.return_value
        mock_cli.insert_records_with_options.return_value = _make_response(_make_body(value=[]))
        api = self._make_api()
        api.insert_records([{"x": 1}], operator_id="op_override")
        req = mock_cli.insert_records_with_options.call_args[0][2]
        self.assertEqual(req.operator_id, "op_override")

    def test_insert_missing_operator_raises(self):
        """未提供 operator_id 时抛 ValueError"""
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "b", "table_name": "s",
        }):
            api = DingTalkAITable()  # 不传 operator_id
        with self.assertRaises(ValueError):
            api.insert_records([{"x": 1}])


# ---------------------------------------------------------------------------
# 5. 列出记录测试
# ---------------------------------------------------------------------------

class TestListRecords(unittest.TestCase):

    def _make_api(self):
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
        }):
            return DingTalkAITable(operator_id="op_001")

    def _mock_record(self, rid, fields):
        r = MagicMock()
        r.id = rid
        r.fields = fields
        r.created_time = 1700000000000
        r.last_modified_time = 1700000000000
        return r

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_list_basic(self, MockNotable, _mock_token):
        """基本分页查询"""
        mock_cli = MockNotable.return_value
        mock_cli.list_records_with_options.return_value = _make_response(_make_body(
            has_more=False,
            next_token=None,
            records=[self._mock_record("r1", {"标题": "a"})],
        ))
        api = self._make_api()
        result = api.list_records(max_results=50)
        self.assertFalse(result["hasMore"])
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(result["records"][0]["id"], "r1")
        self.assertEqual(result["records"][0]["fields"], {"标题": "a"})

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_list_with_filter(self, MockNotable, _mock_token):
        """带筛选条件查询"""
        mock_cli = MockNotable.return_value
        mock_cli.list_records_with_options.return_value = _make_response(_make_body(
            has_more=False, next_token=None, records=[],
        ))
        api = self._make_api()
        api.list_records(filter_conditions=[
            {"field": "数字", "operator": "equal", "value": [100]},
        ])
        req = mock_cli.list_records_with_options.call_args[0][2]
        self.assertIsNotNone(req.filter)
        self.assertEqual(req.filter.combination, "and")
        self.assertEqual(len(req.filter.conditions), 1)
        self.assertEqual(req.filter.conditions[0].field, "数字")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_list_with_field_selection(self, MockNotable, _mock_token):
        """指定返回字段"""
        mock_cli = MockNotable.return_value
        mock_cli.list_records_with_options.return_value = _make_response(_make_body(
            has_more=False, next_token=None, records=[],
        ))
        api = self._make_api()
        api.list_records(field_id_or_names=["标题", "数字"])
        req = mock_cli.list_records_with_options.call_args[0][2]
        self.assertEqual(req.field_id_or_names, ["标题", "数字"])

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_list_all_auto_pagination(self, MockNotable, _mock_token):
        """list_all_records 自动翻页"""
        mock_cli = MockNotable.return_value
        # 第一页有更多，第二页结束
        mock_cli.list_records_with_options.side_effect = [
            _make_response(_make_body(
                has_more=True, next_token="tok_page2",
                records=[self._mock_record("r1", {"n": 1})],
            )),
            _make_response(_make_body(
                has_more=False, next_token=None,
                records=[self._mock_record("r2", {"n": 2})],
            )),
        ]
        api = self._make_api()
        all_recs = api.list_all_records()
        self.assertEqual(len(all_recs), 2)
        self.assertEqual(all_recs[0]["id"], "r1")
        self.assertEqual(all_recs[1]["id"], "r2")
        self.assertEqual(mock_cli.list_records_with_options.call_count, 2)
        # 第二次调用应传入 next_token
        second_req = mock_cli.list_records_with_options.call_args_list[1][0][2]
        self.assertEqual(second_req.next_token, "tok_page2")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_list_calc_fields_true(self, MockNotable, _mock_token):
        """calc_fields=True 时请求体携带 calcFields=true"""
        mock_cli = MockNotable.return_value
        mock_cli.list_records_with_options.return_value = _make_response(_make_body(
            has_more=False, next_token=None, records=[],
        ))
        api = self._make_api()
        api.list_records(calc_fields=True)
        req = mock_cli.list_records_with_options.call_args[0][2]
        self.assertTrue(req.calc_fields)

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_list_calc_fields_default_false(self, MockNotable, _mock_token):
        """calc_fields 默认为 False（不传时）"""
        mock_cli = MockNotable.return_value
        mock_cli.list_records_with_options.return_value = _make_response(_make_body(
            has_more=False, next_token=None, records=[],
        ))
        api = self._make_api()
        api.list_records()
        req = mock_cli.list_records_with_options.call_args[0][2]
        self.assertFalse(req.calc_fields)

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_list_all_calc_fields_propagated(self, MockNotable, _mock_token):
        """list_all_records 的 calc_fields 透传到每一页"""
        mock_cli = MockNotable.return_value
        mock_cli.list_records_with_options.side_effect = [
            _make_response(_make_body(has_more=True, next_token="t2", records=[])),
            _make_response(_make_body(has_more=False, next_token=None, records=[])),
        ]
        api = self._make_api()
        api.list_all_records(calc_fields=True)
        for call in mock_cli.list_records_with_options.call_args_list:
            self.assertTrue(call[0][2].calc_fields)

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_list_empty_result(self, MockNotable, _mock_token):
        """空结果处理"""
        mock_cli = MockNotable.return_value
        mock_cli.list_records_with_options.return_value = _make_response(_make_body(
            has_more=False, next_token=None, records=None,
        ))
        api = self._make_api()
        result = api.list_records()
        self.assertEqual(result["records"], [])


# ---------------------------------------------------------------------------
# 6. 获取单行记录测试
# ---------------------------------------------------------------------------

class TestGetRecord(unittest.TestCase):

    def _make_api(self):
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
        }):
            return DingTalkAITable(operator_id="op_001")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_get_record_success(self, MockNotable, _mock_token):
        """按ID获取单行"""
        mock_cli = MockNotable.return_value
        mock_cli.get_record_with_options.return_value = _make_response(_make_body(
            id="rec_001",
            fields={"标题": "test", "数字": 42},
            created_time=1700000000000,
            last_modified_time=1700000000000,
        ))
        api = self._make_api()
        rec = api.get_record("rec_001")
        self.assertEqual(rec["id"], "rec_001")
        self.assertEqual(rec["fields"]["数字"], 42)
        # 校验路径参数：base_id, sheet, record_id
        call_args = mock_cli.get_record_with_options.call_args[0]
        self.assertEqual(call_args[0], "base1")
        self.assertEqual(call_args[1], "sheet1")
        self.assertEqual(call_args[2], "rec_001")


# ---------------------------------------------------------------------------
# 7. 更新记录测试
# ---------------------------------------------------------------------------

class TestUpdateRecords(unittest.TestCase):

    def _make_api(self):
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
        }):
            return DingTalkAITable(operator_id="op_001")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_update_single(self, MockNotable, _mock_token):
        """更新单条记录"""
        mock_cli = MockNotable.return_value
        mock_cli.update_records_with_options.return_value = _make_response(
            _make_body(value=[MagicMock(id="rec_001")])
        )
        api = self._make_api()
        ids = api.update_records([{"id": "rec_001", "fields": {"数字": 999}}])
        self.assertEqual(ids, ["rec_001"])
        req = mock_cli.update_records_with_options.call_args[0][2]
        self.assertEqual(len(req.records), 1)
        self.assertEqual(req.records[0].id, "rec_001")
        self.assertEqual(req.records[0].fields, {"数字": 999})

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_update_multiple(self, MockNotable, _mock_token):
        """批量更新"""
        mock_cli = MockNotable.return_value
        mock_cli.update_records_with_options.return_value = _make_response(
            _make_body(value=[MagicMock(id="r1"), MagicMock(id="r2")])
        )
        api = self._make_api()
        ids = api.update_records([
            {"id": "r1", "fields": {"a": 1}},
            {"id": "r2", "fields": {"a": 2}},
        ])
        self.assertEqual(ids, ["r1", "r2"])


# ---------------------------------------------------------------------------
# 8. 删除记录测试
# ---------------------------------------------------------------------------

class TestDeleteRecords(unittest.TestCase):

    def _make_api(self):
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
        }):
            return DingTalkAITable(operator_id="op_001")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_delete_success(self, MockNotable, _mock_token):
        """删除成功返回 True"""
        mock_cli = MockNotable.return_value
        mock_cli.delete_records_with_options.return_value = _make_response(
            _make_body(success=True)
        )
        api = self._make_api()
        ok = api.delete_records(["rec_001", "rec_002"])
        self.assertTrue(ok)
        req = mock_cli.delete_records_with_options.call_args[0][2]
        self.assertEqual(req.record_ids, ["rec_001", "rec_002"])

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_delete_false(self, MockNotable, _mock_token):
        """删除失败返回 False"""
        mock_cli = MockNotable.return_value
        mock_cli.delete_records_with_options.return_value = _make_response(
            _make_body(success=False)
        )
        api = self._make_api()
        ok = api.delete_records(["rec_001"])
        self.assertFalse(ok)

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_delete_empty_list(self, MockNotable, _mock_token):
        """空列表删除"""
        mock_cli = MockNotable.return_value
        mock_cli.delete_records_with_options.return_value = _make_response(
            _make_body(success=True)
        )
        api = self._make_api()
        ok = api.delete_records([])
        self.assertTrue(ok)


# ---------------------------------------------------------------------------
# 9. 异常 / 边界测试
# ---------------------------------------------------------------------------

class TestEdgeCases(unittest.TestCase):

    def test_missing_operator_all_methods(self):
        """所有写操作在缺少 operator_id 时均抛 ValueError"""
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "b", "table_name": "s",
        }):
            api = DingTalkAITable()

        with self.assertRaises(ValueError):
            api.insert_records([{"x": 1}])
        with self.assertRaises(ValueError):
            api.list_records()
        with self.assertRaises(ValueError):
            api.get_record("r1")
        with self.assertRaises(ValueError):
            api.update_records([{"id": "r1", "fields": {}}])
        with self.assertRaises(ValueError):
            api.delete_records(["r1"])

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_insert_empty_records(self, MockNotable, _mock_token):
        """空列表新增不报错"""
        mock_cli = MockNotable.return_value
        mock_cli.insert_records_with_options.return_value = _make_response(_make_body(value=[]))
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "b", "table_name": "s",
        }):
            api = DingTalkAITable(operator_id="op")
        ids = api.insert_records([])
        self.assertEqual(ids, [])

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_sdk_error_propagates(self, MockNotable, _mock_token):
        """SDK 异常应原样抛出，不被吞掉"""
        mock_cli = MockNotable.return_value
        mock_cli.insert_records_with_options.side_effect = RuntimeError("network error")
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "b", "table_name": "s",
        }):
            api = DingTalkAITable(operator_id="op")
        with self.assertRaises(RuntimeError):
            api.insert_records([{"x": 1}])


# ---------------------------------------------------------------------------
# 10. 请求头与查询参数验证（核心需求：每次请求自动携带 token 和 operatorId）
# ---------------------------------------------------------------------------

class TestRequestHeadersAndParams(unittest.TestCase):
    """验证每个 API 调用时：
    - 请求头 x-acs-dingtalk-access-token 自动携带 token 值
    - URL 查询参数 operatorId 自动携带操作人 unionId
    """

    def _make_api(self):
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
        }):
            return DingTalkAITable(operator_id="op_default")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok_abc")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_insert_headers_and_operator(self, MockNotable, _mock_token):
        """新增：请求头带 token，查询参数带 operatorId"""
        mock_cli = MockNotable.return_value
        mock_cli.insert_records_with_options.return_value = _make_response(_make_body(value=[]))
        api = self._make_api()
        api.insert_records([{"标题": "x"}], operator_id="op_001")

        call = mock_cli.insert_records_with_options.call_args[0]
        base_id, sheet, req, headers, runtime = call
        self.assertEqual(base_id, "base1")
        self.assertEqual(sheet, "sheet1")
        self.assertEqual(req.operator_id, "op_001")          # → query operatorId
        self.assertEqual(headers.x_acs_dingtalk_access_token, "tok_abc")  # → header

    @patch("dingtalk_ai_table.get_access_token", return_value="tok_abc")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_list_headers_and_operator(self, MockNotable, _mock_token):
        """查询列表：请求头带 token，查询参数带 operatorId"""
        mock_cli = MockNotable.return_value
        mock_cli.list_records_with_options.return_value = _make_response(_make_body(
            has_more=False, next_token=None, records=[],
        ))
        api = self._make_api()
        api.list_records(operator_id="op_002")

        call = mock_cli.list_records_with_options.call_args[0]
        base_id, sheet, req, headers, runtime = call
        self.assertEqual(req.operator_id, "op_002")
        self.assertEqual(headers.x_acs_dingtalk_access_token, "tok_abc")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok_abc")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_get_record_headers_and_operator(self, MockNotable, _mock_token):
        """获取单行：请求头带 token，查询参数带 operatorId"""
        mock_cli = MockNotable.return_value
        mock_cli.get_record_with_options.return_value = _make_response(_make_body(
            id="r1", fields={}, created_time=1, last_modified_time=1,
        ))
        api = self._make_api()
        api.get_record("rec_001", operator_id="op_003")

        call = mock_cli.get_record_with_options.call_args[0]
        base_id, sheet, record_id, req, headers, runtime = call
        self.assertEqual(record_id, "rec_001")
        self.assertEqual(req.operator_id, "op_003")
        self.assertEqual(headers.x_acs_dingtalk_access_token, "tok_abc")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok_abc")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_update_headers_and_operator(self, MockNotable, _mock_token):
        """更新：请求头带 token，查询参数带 operatorId"""
        mock_cli = MockNotable.return_value
        mock_cli.update_records_with_options.return_value = _make_response(_make_body(value=[]))
        api = self._make_api()
        api.update_records([{"id": "r1", "fields": {"a": 1}}], operator_id="op_004")

        call = mock_cli.update_records_with_options.call_args[0]
        base_id, sheet, req, headers, runtime = call
        self.assertEqual(req.operator_id, "op_004")
        self.assertEqual(headers.x_acs_dingtalk_access_token, "tok_abc")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok_abc")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_delete_headers_and_operator(self, MockNotable, _mock_token):
        """删除：请求头带 token，查询参数带 operatorId"""
        mock_cli = MockNotable.return_value
        mock_cli.delete_records_with_options.return_value = _make_response(_make_body(success=True))
        api = self._make_api()
        api.delete_records(["r1"], operator_id="op_005")

        call = mock_cli.delete_records_with_options.call_args[0]
        base_id, sheet, req, headers, runtime = call
        self.assertEqual(req.operator_id, "op_005")
        self.assertEqual(req.record_ids, ["r1"])
        self.assertEqual(headers.x_acs_dingtalk_access_token, "tok_abc")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok_default")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_default_operator_from_init(self, MockNotable, _mock_token):
        """未显式传 operator_id 时，使用初始化时的 operator_id"""
        mock_cli = MockNotable.return_value
        mock_cli.list_records_with_options.return_value = _make_response(_make_body(
            has_more=False, next_token=None, records=[],
        ))
        api = self._make_api()  # operator_id="op_default"
        api.list_records()

        req = mock_cli.list_records_with_options.call_args[0][2]
        self.assertEqual(req.operator_id, "op_default")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok_fresh")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_token_carried_in_headers_every_call(self, MockNotable, _mock_token):
        """每次调用的请求头中都携带 token 值"""
        mock_cli = MockNotable.return_value
        mock_cli.list_records_with_options.return_value = _make_response(_make_body(
            has_more=False, next_token=None, records=[],
        ))
        api = self._make_api()
        api.list_records()
        api.list_records()

        first_headers = mock_cli.list_records_with_options.call_args_list[0][0][3]
        second_headers = mock_cli.list_records_with_options.call_args_list[1][0][3]
        self.assertEqual(first_headers.x_acs_dingtalk_access_token, "tok_fresh")
        self.assertEqual(second_headers.x_acs_dingtalk_access_token, "tok_fresh")


# ---------------------------------------------------------------------------
# 11. 异步方法测试
# ---------------------------------------------------------------------------

class TestAsyncMethods(unittest.IsolatedAsyncioTestCase):
    """异步 CRUD 方法测试（mock SDK async 接口）。"""

    def _make_api(self):
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
        }):
            return DingTalkAITable(operator_id="op_001")

    @patch("dingtalk_ai_table.get_access_token_async", new_callable=AsyncMock)
    @patch("dingtalk_ai_table.notable_client.Client")
    async def test_insert_async(self, MockNotable, _mock_token):
        """异步新增"""
        _mock_token.return_value = "tok"
        mock_cli = MockNotable.return_value
        mock_cli.insert_records_with_options_async = AsyncMock(return_value=_make_response(
            _make_body(value=[MagicMock(id="rec_async_1")])
        ))
        api = self._make_api()
        ids = await api.insert_records_async([{"标题": "async"}])
        self.assertEqual(ids, ["rec_async_1"])
        mock_cli.insert_records_with_options_async.assert_called_once()
        # 验证 headers 携带 token
        call_args = mock_cli.insert_records_with_options_async.call_args[0]
        self.assertEqual(call_args[3].x_acs_dingtalk_access_token, "tok")
        self.assertEqual(call_args[2].operator_id, "op_001")

    @patch("dingtalk_ai_table.get_access_token_async", new_callable=AsyncMock)
    @patch("dingtalk_ai_table.notable_client.Client")
    async def test_list_async(self, MockNotable, _mock_token):
        """异步查询列表"""
        _mock_token.return_value = "tok"
        mock_cli = MockNotable.return_value
        r = MagicMock()
        r.id = "ra1"; r.fields = {"x": 1}; r.created_time = 1; r.last_modified_time = 1
        mock_cli.list_records_with_options_async = AsyncMock(return_value=_make_response(_make_body(
            has_more=False, next_token=None, records=[r],
        )))
        api = self._make_api()
        result = await api.list_records_async(max_results=10, calc_fields=True)
        self.assertEqual(len(result["records"]), 1)
        req = mock_cli.list_records_with_options_async.call_args[0][2]
        self.assertTrue(req.calc_fields)

    @patch("dingtalk_ai_table.get_access_token_async", new_callable=AsyncMock)
    @patch("dingtalk_ai_table.notable_client.Client")
    async def test_get_record_async(self, MockNotable, _mock_token):
        """异步获取单行"""
        _mock_token.return_value = "tok"
        mock_cli = MockNotable.return_value
        mock_cli.get_record_with_options_async = AsyncMock(return_value=_make_response(_make_body(
            id="r1", fields={"a": 1}, created_time=1, last_modified_time=1,
        )))
        api = self._make_api()
        rec = await api.get_record_async("r1")
        self.assertEqual(rec["id"], "r1")
        self.assertEqual(rec["fields"], {"a": 1})

    @patch("dingtalk_ai_table.get_access_token_async", new_callable=AsyncMock)
    @patch("dingtalk_ai_table.notable_client.Client")
    async def test_update_async(self, MockNotable, _mock_token):
        """异步更新"""
        _mock_token.return_value = "tok"
        mock_cli = MockNotable.return_value
        mock_cli.update_records_with_options_async = AsyncMock(return_value=_make_response(
            _make_body(value=[MagicMock(id="r1")])
        ))
        api = self._make_api()
        ids = await api.update_records_async([{"id": "r1", "fields": {"a": 2}}])
        self.assertEqual(ids, ["r1"])

    @patch("dingtalk_ai_table.get_access_token_async", new_callable=AsyncMock)
    @patch("dingtalk_ai_table.notable_client.Client")
    async def test_delete_async(self, MockNotable, _mock_token):
        """异步删除"""
        _mock_token.return_value = "tok"
        mock_cli = MockNotable.return_value
        mock_cli.delete_records_with_options_async = AsyncMock(return_value=_make_response(
            _make_body(success=True)
        ))
        api = self._make_api()
        ok = await api.delete_records_async(["r1", "r2"])
        self.assertTrue(ok)

    @patch("dingtalk_ai_table.get_access_token_async", new_callable=AsyncMock)
    @patch("dingtalk_ai_table.notable_client.Client")
    async def test_concurrent_async_calls(self, MockNotable, _mock_token):
        """并发异步调用：多个协程同时执行不冲突"""
        _mock_token.return_value = "tok"
        mock_cli = MockNotable.return_value
        mock_cli.get_record_with_options_async = AsyncMock(return_value=_make_response(_make_body(
            id="r", fields={}, created_time=1, last_modified_time=1,
        )))
        api = self._make_api()
        # 并发发起3个异步查询
        results = await asyncio.gather(
            api.get_record_async("r1"),
            api.get_record_async("r2"),
            api.get_record_async("r3"),
        )
        self.assertEqual(len(results), 3)
        self.assertEqual(mock_cli.get_record_with_options_async.call_count, 3)


# ---------------------------------------------------------------------------
# 12. 重试机制测试
# ---------------------------------------------------------------------------

class TestRetryMechanism(unittest.TestCase):
    """重试机制测试。"""

    def _make_api(self, max_retries=10):
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
        }):
            return DingTalkAITable(operator_id="op_001", max_retries=max_retries)

    def _make_tea_error(self, status_code, code="error", message="err"):
        """构造真实的 TeaException 异常。"""
        from darabonba.exceptions import TeaException
        return TeaException({
            "code": code, "message": message,
            "data": {"statusCode": status_code},
        })

    def test_default_max_retries_is_10(self):
        """默认重试次数为10"""
        api = self._make_api()
        self.assertEqual(api.max_retries, 10)

    def test_default_retry_delay_is_5(self):
        """默认每次重试间隔为5秒"""
        api = self._make_api()
        self.assertEqual(api.retry_delay, 5.0)

    def test_custom_max_retries(self):
        """可自定义重试次数"""
        api = self._make_api(max_retries=5)
        self.assertEqual(api.max_retries, 5)

    def test_zero_retries_no_retry(self):
        """max_retries=0 时不重试"""
        api = self._make_api(max_retries=0)
        self.assertEqual(api.max_retries, 0)

    @patch("dingtalk_ai_table.time.sleep")
    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_retry_on_5xx_error(self, MockNotable, _mock_token, _mock_sleep):
        """5xx错误触发重试，最终成功"""
        mock_cli = MockNotable.return_value
        err = self._make_tea_error(500, "internalError", "server error")
        mock_cli.list_records_with_options.side_effect = [
            err, err,
            _make_response(_make_body(has_more=False, next_token=None, records=[])),
        ]
        api = self._make_api(max_retries=3)
        result = api.list_records(max_results=10)
        self.assertEqual(result["records"], [])
        self.assertEqual(mock_cli.list_records_with_options.call_count, 3)

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_no_retry_on_4xx_error(self, MockNotable, _mock_token):
        """4xx错误不重试，直接抛出"""
        from darabonba.exceptions import TeaException
        mock_cli = MockNotable.return_value
        mock_cli.list_records_with_options.side_effect = self._make_tea_error(400, "invalidRequest")
        api = self._make_api(max_retries=3)
        with self.assertRaises(TeaException):
            api.list_records(max_results=10)
        self.assertEqual(mock_cli.list_records_with_options.call_count, 1)

    @patch("dingtalk_ai_table.time.sleep")
    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_retry_exhausted_raises(self, MockNotable, _mock_token, _mock_sleep):
        """重试次数耗尽后抛出异常"""
        from darabonba.exceptions import TeaException
        mock_cli = MockNotable.return_value
        mock_cli.list_records_with_options.side_effect = self._make_tea_error(503, "service.timeout")
        api = self._make_api(max_retries=2)
        with self.assertRaises(TeaException):
            api.list_records(max_results=10)
        # 首次1次 + 重试2次 = 3次调用
        self.assertEqual(mock_cli.list_records_with_options.call_count, 3)

    @patch("dingtalk_ai_table.time.sleep")
    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_retry_fixed_delay(self, MockNotable, _mock_token, mock_sleep):
        """每次重试等待固定时间 retry_delay（默认5s）"""
        mock_cli = MockNotable.return_value
        err = self._make_tea_error(500, "internalError")
        mock_cli.list_records_with_options.side_effect = [
            err, err,
            _make_response(_make_body(has_more=False, next_token=None, records=[])),
        ]
        api = self._make_api(max_retries=3)
        api.list_records(max_results=10)
        sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
        self.assertEqual(sleep_calls, [5.0, 5.0])

    @patch("dingtalk_ai_table.time.sleep")
    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_custom_retry_delay(self, MockNotable, _mock_token, mock_sleep):
        """自定义 retry_delay 生效"""
        mock_cli = MockNotable.return_value
        err = self._make_tea_error(500, "internalError")
        mock_cli.list_records_with_options.side_effect = [
            err,
            _make_response(_make_body(has_more=False, next_token=None, records=[])),
        ]
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
        }):
            api = DingTalkAITable(operator_id="op_001", max_retries=3, retry_delay=5)
        api.list_records(max_results=10)
        self.assertEqual(mock_sleep.call_args[0][0], 5.0)


# ---------------------------------------------------------------------------
# 13. 版本检查测试
# ---------------------------------------------------------------------------

class TestSDKVersionCheck(unittest.TestCase):
    """SDK 版本检查测试。"""

    def test_parse_version(self):
        """版本号解析正确"""
        self.assertEqual(m._parse_version("2.2.57"), (2, 2, 57))
        self.assertEqual(m._parse_version("2.2.56"), (2, 2, 56))
        self.assertEqual(m._parse_version("3.0.0"), (3, 0, 0))

    def test_version_comparison(self):
        """版本比较逻辑正确"""
        self.assertTrue(m._parse_version("2.2.57") >= m._parse_version("2.2.57"))
        self.assertTrue(m._parse_version("2.2.58") >= m._parse_version("2.2.57"))
        self.assertTrue(m._parse_version("2.3.0") >= m._parse_version("2.2.57"))
        self.assertTrue(m._parse_version("3.0.0") >= m._parse_version("2.2.57"))
        self.assertFalse(m._parse_version("2.2.56") >= m._parse_version("2.2.57"))
        self.assertFalse(m._parse_version("2.1.99") >= m._parse_version("2.2.57"))

    @patch("dingtalk_ai_table._pkg_version", return_value="2.2.57")
    def test_version_ok(self, _mock):
        """版本 >= 2.2.57 时检查通过"""
        m._check_sdk_version()  # 不抛异常即通过

    @patch("dingtalk_ai_table._pkg_version", return_value="2.2.56")
    def test_version_too_low_raises(self, _mock):
        """版本 < 2.2.57 时抛 ImportError"""
        with self.assertRaises(ImportError) as ctx:
            m._check_sdk_version()
        self.assertIn("2.2.57", str(ctx.exception))

    @patch("dingtalk_ai_table._pkg_version", side_effect=m.PackageNotFoundError)
    def test_version_not_installed_raises(self, _mock):
        """未安装 SDK 时抛 ImportError"""
        with self.assertRaises(ImportError):
            m._check_sdk_version()

    @patch("dingtalk_ai_table._check_sdk_version")
    def test_init_calls_version_check(self, mock_check):
        """初始化时调用版本检查"""
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
        }):
            DingTalkAITable(operator_id="op")
        mock_check.assert_called_once()


# ---------------------------------------------------------------------------
# 14. 批量拆分测试
# ---------------------------------------------------------------------------

class TestBatchSplitting(unittest.TestCase):
    """批量拆分（insert 200/批，update/delete 100/批）测试。"""

    def _make_api(self):
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
        }):
            return DingTalkAITable(operator_id="op_001")

    def test_chunk_list(self):
        """_chunk_list 按指定大小拆分"""
        self.assertEqual(m._chunk_list([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])
        self.assertEqual(m._chunk_list([1, 2, 3], 10), [[1, 2, 3]])
        self.assertEqual(m._chunk_list([], 5), [])

    def test_batch_constants(self):
        """批量上限常量正确"""
        self.assertEqual(m.INSERT_BATCH_SIZE, 200)
        self.assertEqual(m.UPDATE_BATCH_SIZE, 100)
        self.assertEqual(m.DELETE_BATCH_SIZE, 100)

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_insert_splits_over_200(self, MockNotable, _mock_token):
        """新增超过200条时自动拆分为多批"""
        mock_cli = MockNotable.return_value
        # 每批返回对应数量的ID
        def mock_insert(*args, **kwargs):
            req = args[2]
            n = len(req.records)
            return _make_response(_make_body(
                value=[MagicMock(id="id_%d" % i) for i in range(n)]
            ))
        mock_cli.insert_records_with_options.side_effect = mock_insert

        api = self._make_api()
        records = [{"x": i} for i in range(250)]  # 250条 → 200 + 50
        ids = api.insert_records(records)
        self.assertEqual(len(ids), 250)
        self.assertEqual(mock_cli.insert_records_with_options.call_count, 2)

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_insert_exactly_200_no_split(self, MockNotable, _mock_token):
        """恰好200条不拆分"""
        mock_cli = MockNotable.return_value
        mock_cli.insert_records_with_options.return_value = _make_response(_make_body(
            value=[MagicMock(id="id_%d" % i) for i in range(200)]
        ))
        api = self._make_api()
        ids = api.insert_records([{"x": i} for i in range(200)])
        self.assertEqual(len(ids), 200)
        self.assertEqual(mock_cli.insert_records_with_options.call_count, 1)

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_update_splits_over_100(self, MockNotable, _mock_token):
        """更新超过100条时自动拆分为多批"""
        mock_cli = MockNotable.return_value
        def mock_update(*args, **kwargs):
            req = args[2]
            n = len(req.records)
            return _make_response(_make_body(
                value=[MagicMock(id="u_%d" % i) for i in range(n)]
            ))
        mock_cli.update_records_with_options.side_effect = mock_update

        api = self._make_api()
        records = [{"id": "r%d" % i, "fields": {"x": i}} for i in range(150)]
        ids = api.update_records(records)
        self.assertEqual(len(ids), 150)
        self.assertEqual(mock_cli.update_records_with_options.call_count, 2)

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_delete_splits_over_100(self, MockNotable, _mock_token):
        """删除超过100条时自动拆分为多批"""
        mock_cli = MockNotable.return_value
        mock_cli.delete_records_with_options.return_value = _make_response(_make_body(success=True))

        api = self._make_api()
        ids = ["r%d" % i for i in range(150)]
        ok = api.delete_records(ids)
        self.assertTrue(ok)
        self.assertEqual(mock_cli.delete_records_with_options.call_count, 2)

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_empty_insert_returns_empty(self, MockNotable, _mock_token):
        """空列表新增不调用SDK，返回空"""
        mock_cli = MockNotable.return_value
        api = self._make_api()
        result = api.insert_records([])
        self.assertEqual(result, [])
        mock_cli.insert_records_with_options.assert_not_called()

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_empty_delete_returns_true(self, MockNotable, _mock_token):
        """空列表删除不调用SDK，返回True"""
        mock_cli = MockNotable.return_value
        api = self._make_api()
        ok = api.delete_records([])
        self.assertTrue(ok)
        mock_cli.delete_records_with_options.assert_not_called()


# ---------------------------------------------------------------------------
# 15. 多选字段测试
# ---------------------------------------------------------------------------

class TestMultiSelectField(unittest.TestCase):
    """多选字段（multiSelect）操作测试。"""

    def _make_api(self):
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
        }):
            return DingTalkAITable(operator_id="op_001")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_insert_multi_select_array(self, MockNotable, _mock_token):
        """新增时多选字段传字符串数组，透传到SDK"""
        mock_cli = MockNotable.return_value
        mock_cli.insert_records_with_options.return_value = _make_response(
            _make_body(value=[MagicMock(id="rec_001")])
        )
        api = self._make_api()
        api.insert_records([{"标题": "x", "标签": ["选项A", "选项B"]}])
        req = mock_cli.insert_records_with_options.call_args[0][2]
        self.assertEqual(req.records[0].fields["标签"], ["选项A", "选项B"])

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_insert_multi_select_empty(self, MockNotable, _mock_token):
        """新增时多选字段传空数组"""
        mock_cli = MockNotable.return_value
        mock_cli.insert_records_with_options.return_value = _make_response(
            _make_body(value=[MagicMock(id="rec_001")])
        )
        api = self._make_api()
        api.insert_records([{"标题": "x", "标签": []}])
        req = mock_cli.insert_records_with_options.call_args[0][2]
        self.assertEqual(req.records[0].fields["标签"], [])

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_update_multi_select_replace(self, MockNotable, _mock_token):
        """更新多选字段：传新数组替换原值"""
        mock_cli = MockNotable.return_value
        mock_cli.update_records_with_options.return_value = _make_response(
            _make_body(value=[MagicMock(id="rec_001")])
        )
        api = self._make_api()
        api.update_records([{"id": "rec_001", "fields": {"标签": ["选项C"]}}])
        req = mock_cli.update_records_with_options.call_args[0][2]
        self.assertEqual(req.records[0].fields["标签"], ["选项C"])

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_update_multi_select_clear(self, MockNotable, _mock_token):
        """更新多选字段：传空数组清空"""
        mock_cli = MockNotable.return_value
        mock_cli.update_records_with_options.return_value = _make_response(
            _make_body(value=[MagicMock(id="rec_001")])
        )
        api = self._make_api()
        api.update_records([{"id": "rec_001", "fields": {"标签": []}}])
        req = mock_cli.update_records_with_options.call_args[0][2]
        self.assertEqual(req.records[0].fields["标签"], [])

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_get_record_returns_multi_select_objects(self, MockNotable, _mock_token):
        """读取时多选字段返回对象数组（含name和id）"""
        mock_cli = MockNotable.return_value
        mock_cli.get_record_with_options.return_value = _make_response(_make_body(
            id="rec_001",
            fields={"标签": [{"name": "选项A", "id": "id_a"}, {"name": "选项B", "id": "id_b"}]},
            created_time=1, last_modified_time=1,
        ))
        api = self._make_api()
        rec = api.get_record("rec_001")
        self.assertEqual(len(rec["fields"]["标签"]), 2)
        self.assertEqual(rec["fields"]["标签"][0]["name"], "选项A")
        self.assertEqual(rec["fields"]["标签"][1]["id"], "id_b")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_filter_multi_select_equal(self, MockNotable, _mock_token):
        """多选字段 equal 筛选：value 为字符串数组（完全匹配）"""
        mock_cli = MockNotable.return_value
        mock_cli.list_records_with_options.return_value = _make_response(_make_body(
            has_more=False, next_token=None, records=[],
        ))
        api = self._make_api()
        api.list_records(filter_conditions=[
            {"field": "标签", "operator": "=", "value": ["选项A"]},
        ])
        req = mock_cli.list_records_with_options.call_args[0][2]
        self.assertEqual(req.filter.conditions[0].operator, "equal")
        self.assertEqual(req.filter.conditions[0].value, ["选项A"])

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_filter_multi_select_not_equal(self, MockNotable, _mock_token):
        """多选字段 notEqual 筛选"""
        mock_cli = MockNotable.return_value
        mock_cli.list_records_with_options.return_value = _make_response(_make_body(
            has_more=False, next_token=None, records=[],
        ))
        api = self._make_api()
        api.list_records(filter_conditions=[
            {"field": "标签", "operator": "!=", "value": ["选项A"]},
        ])
        req = mock_cli.list_records_with_options.call_args[0][2]
        self.assertEqual(req.filter.conditions[0].operator, "notEqual")


# ---------------------------------------------------------------------------
# 16. 字段管理测试（Field CRUD）
# ---------------------------------------------------------------------------

class TestFieldManagement(unittest.TestCase):
    """字段（列）增删查改测试。"""

    def _make_api(self):
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
        }):
            return DingTalkAITable(operator_id="op_001")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_create_field(self, MockNotable, _mock_token):
        """创建字段：text类型"""
        mock_cli = MockNotable.return_value
        mock_cli.create_field_with_options.return_value = _make_response(_make_body(
            id="fld_001", name="新字段", type="text", property=None,
        ))
        api = self._make_api()
        result = api.create_field(name="新字段", field_type="text")
        self.assertEqual(result["id"], "fld_001")
        self.assertEqual(result["name"], "新字段")
        self.assertEqual(result["type"], "text")
        # 校验请求参数
        req = mock_cli.create_field_with_options.call_args[0][2]
        self.assertEqual(req.name, "新字段")
        self.assertEqual(req.type, "text")
        self.assertEqual(req.operator_id, "op_001")
        # 校验请求头
        headers = mock_cli.create_field_with_options.call_args[0][3]
        self.assertEqual(headers.x_acs_dingtalk_access_token, "tok")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_create_field_with_property(self, MockNotable, _mock_token):
        """创建字段：带property（如单选options）"""
        mock_cli = MockNotable.return_value
        mock_cli.create_field_with_options.return_value = _make_response(_make_body(
            id="fld_002", name="优先级", type="singleSelect", property={"choices": []},
        ))
        api = self._make_api()
        result = api.create_field(
            name="优先级", field_type="singleSelect",
            property={"options": [{"name": "高"}, {"name": "低"}]},
        )
        self.assertEqual(result["type"], "singleSelect")
        req = mock_cli.create_field_with_options.call_args[0][2]
        self.assertEqual(req.property, {"options": [{"name": "高"}, {"name": "低"}]})

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_list_fields(self, MockNotable, _mock_token):
        """获取所有字段"""
        mock_cli = MockNotable.return_value
        f1 = MagicMock()
        f1.id = "f1"; f1.name = "字段1"; f1.type = "text"; f1.property = None
        f2 = MagicMock()
        f2.id = "f2"; f2.name = "字段2"; f2.type = "number"; f2.property = {"fmt": "0"}
        mock_cli.get_all_fields_with_options.return_value = _make_response(_make_body(
            value=[f1, f2],
        ))
        api = self._make_api()
        fields = api.list_fields()
        self.assertEqual(len(fields), 2)
        self.assertEqual(fields[0]["name"], "字段1")
        self.assertEqual(fields[1]["type"], "number")
        # 校验请求头和operator
        req = mock_cli.get_all_fields_with_options.call_args[0][2]
        self.assertEqual(req.operator_id, "op_001")
        headers = mock_cli.get_all_fields_with_options.call_args[0][3]
        self.assertEqual(headers.x_acs_dingtalk_access_token, "tok")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_update_field(self, MockNotable, _mock_token):
        """更新字段：重命名"""
        mock_cli = MockNotable.return_value
        mock_cli.update_field_with_options.return_value = _make_response(_make_body(
            id="fld_001",
        ))
        api = self._make_api()
        result = api.update_field(field_id_or_name="fld_001", name="新名称")
        self.assertEqual(result, "fld_001")
        # 校验路径参数：base_id, sheet, field_id_or_name
        call_args = mock_cli.update_field_with_options.call_args[0]
        self.assertEqual(call_args[0], "base1")
        self.assertEqual(call_args[1], "sheet1")
        self.assertEqual(call_args[2], "fld_001")
        req = call_args[3]
        self.assertEqual(req.name, "新名称")
        self.assertEqual(req.operator_id, "op_001")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_update_field_by_name(self, MockNotable, _mock_token):
        """更新字段：按字段名更新"""
        mock_cli = MockNotable.return_value
        mock_cli.update_field_with_options.return_value = _make_response(_make_body(id="fld_x"))
        api = self._make_api()
        api.update_field(field_id_or_name="旧字段名", name="新字段名")
        call_args = mock_cli.update_field_with_options.call_args[0]
        self.assertEqual(call_args[2], "旧字段名")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_delete_field(self, MockNotable, _mock_token):
        """删除字段"""
        mock_cli = MockNotable.return_value
        mock_cli.delete_field_with_options.return_value = _make_response(_make_body(success=True))
        api = self._make_api()
        ok = api.delete_field("fld_001")
        self.assertTrue(ok)
        call_args = mock_cli.delete_field_with_options.call_args[0]
        self.assertEqual(call_args[2], "fld_001")
        req = call_args[3]
        self.assertEqual(req.operator_id, "op_001")
        headers = call_args[4]
        self.assertEqual(headers.x_acs_dingtalk_access_token, "tok")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_delete_field_by_name(self, MockNotable, _mock_token):
        """删除字段：按字段名删除"""
        mock_cli = MockNotable.return_value
        mock_cli.delete_field_with_options.return_value = _make_response(_make_body(success=True))
        api = self._make_api()
        ok = api.delete_field("待删除字段")
        self.assertTrue(ok)
        self.assertEqual(mock_cli.delete_field_with_options.call_args[0][2], "待删除字段")


# ---------------------------------------------------------------------------
# 16.5 数据表管理测试（Sheet CRUD）
# ---------------------------------------------------------------------------

class TestSheetManagement(unittest.TestCase):
    """数据表（Sheet）增删查改测试。"""

    def _make_api(self):
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
        }):
            return DingTalkAITable(operator_id="op_001")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_create_sheet(self, MockNotable, _mock_token):
        """创建数据表：仅名称"""
        mock_cli = MockNotable.return_value
        mock_cli.create_sheet_with_options.return_value = _make_response(_make_body(
            id="sheet_001", name="新数据表",
        ))
        api = self._make_api()
        result = api.create_sheet(name="新数据表")
        self.assertEqual(result["id"], "sheet_001")
        self.assertEqual(result["name"], "新数据表")
        # 校验请求参数（create_sheet 不需要 sheet_id_or_name）
        call_args = mock_cli.create_sheet_with_options.call_args[0]
        self.assertEqual(call_args[0], "base1")
        req = call_args[1]
        self.assertEqual(req.name, "新数据表")
        self.assertEqual(req.operator_id, "op_001")
        self.assertIsNone(req.fields)
        # 校验请求头
        headers = call_args[2]
        self.assertEqual(headers.x_acs_dingtalk_access_token, "tok")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_create_sheet_with_fields(self, MockNotable, _mock_token):
        """创建数据表：带初始字段"""
        mock_cli = MockNotable.return_value
        mock_cli.create_sheet_with_options.return_value = _make_response(_make_body(
            id="sheet_002", name="带字段表",
        ))
        api = self._make_api()
        result = api.create_sheet(
            name="带字段表",
            fields=[
                {"name": "标题", "type": "text"},
                {"name": "数量", "type": "number", "property": {"fmt": "0"}},
            ],
        )
        self.assertEqual(result["id"], "sheet_002")
        req = mock_cli.create_sheet_with_options.call_args[0][1]
        self.assertEqual(len(req.fields), 2)
        self.assertEqual(req.fields[0].name, "标题")
        self.assertEqual(req.fields[0].type, "text")
        self.assertEqual(req.fields[1].name, "数量")
        self.assertEqual(req.fields[1].property, {"fmt": "0"})

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_get_sheet(self, MockNotable, _mock_token):
        """获取单个数据表"""
        mock_cli = MockNotable.return_value
        mock_cli.get_sheet_with_options.return_value = _make_response(_make_body(
            id="sheet_001", name="数据表A",
        ))
        api = self._make_api()
        result = api.get_sheet("sheet_001")
        self.assertEqual(result["id"], "sheet_001")
        self.assertEqual(result["name"], "数据表A")
        call_args = mock_cli.get_sheet_with_options.call_args[0]
        self.assertEqual(call_args[0], "base1")
        self.assertEqual(call_args[1], "sheet_001")
        self.assertEqual(call_args[2].operator_id, "op_001")
        self.assertEqual(call_args[3].x_acs_dingtalk_access_token, "tok")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_get_sheet_by_name(self, MockNotable, _mock_token):
        """按名称获取数据表"""
        mock_cli = MockNotable.return_value
        mock_cli.get_sheet_with_options.return_value = _make_response(_make_body(
            id="sheet_x", name="我的表",
        ))
        api = self._make_api()
        api.get_sheet("我的表")
        self.assertEqual(mock_cli.get_sheet_with_options.call_args[0][1], "我的表")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_list_sheets(self, MockNotable, _mock_token):
        """获取所有数据表"""
        mock_cli = MockNotable.return_value
        s1 = MagicMock(); s1.id = "s1"; s1.name = "表1"
        s2 = MagicMock(); s2.id = "s2"; s2.name = "表2"
        mock_cli.get_all_sheets_with_options.return_value = _make_response(_make_body(
            value=[s1, s2],
        ))
        api = self._make_api()
        sheets = api.list_sheets()
        self.assertEqual(len(sheets), 2)
        self.assertEqual(sheets[0]["name"], "表1")
        self.assertEqual(sheets[1]["id"], "s2")
        # list_sheets 不需要 sheet_id_or_name
        call_args = mock_cli.get_all_sheets_with_options.call_args[0]
        self.assertEqual(call_args[0], "base1")
        self.assertEqual(call_args[1].operator_id, "op_001")
        self.assertEqual(call_args[2].x_acs_dingtalk_access_token, "tok")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_list_sheets_empty(self, MockNotable, _mock_token):
        """空数据表列表"""
        mock_cli = MockNotable.return_value
        mock_cli.get_all_sheets_with_options.return_value = _make_response(_make_body(value=None))
        api = self._make_api()
        sheets = api.list_sheets()
        self.assertEqual(sheets, [])

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_update_sheet(self, MockNotable, _mock_token):
        """更新数据表：重命名"""
        mock_cli = MockNotable.return_value
        mock_cli.update_sheet_with_options.return_value = _make_response(_make_body(
            id="sheet_001", name="新名称",
        ))
        api = self._make_api()
        result = api.update_sheet("sheet_001", name="新名称")
        self.assertEqual(result["id"], "sheet_001")
        self.assertEqual(result["name"], "新名称")
        call_args = mock_cli.update_sheet_with_options.call_args[0]
        self.assertEqual(call_args[0], "base1")
        self.assertEqual(call_args[1], "sheet_001")
        self.assertEqual(call_args[2].name, "新名称")
        self.assertEqual(call_args[2].operator_id, "op_001")
        self.assertEqual(call_args[3].x_acs_dingtalk_access_token, "tok")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_delete_sheet(self, MockNotable, _mock_token):
        """删除数据表"""
        mock_cli = MockNotable.return_value
        mock_cli.delete_sheet_with_options.return_value = _make_response(_make_body(success=True))
        api = self._make_api()
        ok = api.delete_sheet("sheet_001")
        self.assertTrue(ok)
        call_args = mock_cli.delete_sheet_with_options.call_args[0]
        self.assertEqual(call_args[0], "base1")
        self.assertEqual(call_args[1], "sheet_001")
        self.assertEqual(call_args[2].operator_id, "op_001")
        self.assertEqual(call_args[3].x_acs_dingtalk_access_token, "tok")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_delete_sheet_by_name(self, MockNotable, _mock_token):
        """按名称删除数据表"""
        mock_cli = MockNotable.return_value
        mock_cli.delete_sheet_with_options.return_value = _make_response(_make_body(success=True))
        api = self._make_api()
        api.delete_sheet("待删除表")
        self.assertEqual(mock_cli.delete_sheet_with_options.call_args[0][1], "待删除表")

    @patch("dingtalk_ai_table.get_access_token", return_value="tok")
    @patch("dingtalk_ai_table.notable_client.Client")
    def test_sheet_operations_use_base_not_sheet(self, MockNotable, _mock_token):
        """create_sheet 和 list_sheets 操作 base 级别，不依赖初始化时的 sheet_id_or_name"""
        mock_cli = MockNotable.return_value
        mock_cli.create_sheet_with_options.return_value = _make_response(_make_body(id="s1", name="n1"))
        mock_cli.get_all_sheets_with_options.return_value = _make_response(_make_body(value=[]))
        api = self._make_api()
        # create_sheet 调用时只有 base_id + request + headers + runtime（4个位置参数）
        api.create_sheet("测试表")
        create_call = mock_cli.create_sheet_with_options.call_args[0]
        self.assertEqual(len(create_call), 4)  # base_id, request, headers, runtime
        # list_sheets 同样
        api.list_sheets()
        list_call = mock_cli.get_all_sheets_with_options.call_args[0]
        self.assertEqual(len(list_call), 4)


class TestSheetManagementAsync(unittest.IsolatedAsyncioTestCase):
    """数据表异步方法测试。"""

    def _make_api(self):
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
        }):
            return DingTalkAITable(operator_id="op_001")

    @patch("dingtalk_ai_table.get_access_token_async", new_callable=AsyncMock)
    @patch("dingtalk_ai_table.notable_client.Client")
    async def test_create_sheet_async(self, MockNotable, _mock_token):
        _mock_token.return_value = "tok"
        mock_cli = MockNotable.return_value
        mock_cli.create_sheet_with_options_async = AsyncMock(return_value=_make_response(_make_body(id="s1", name="异步表")))
        api = self._make_api()
        result = await api.create_sheet_async(name="异步表")
        self.assertEqual(result["id"], "s1")
        mock_cli.create_sheet_with_options_async.assert_called_once()

    @patch("dingtalk_ai_table.get_access_token_async", new_callable=AsyncMock)
    @patch("dingtalk_ai_table.notable_client.Client")
    async def test_list_sheets_async(self, MockNotable, _mock_token):
        _mock_token.return_value = "tok"
        mock_cli = MockNotable.return_value
        s1 = MagicMock(); s1.id = "s1"; s1.name = "表1"
        mock_cli.get_all_sheets_with_options_async = AsyncMock(return_value=_make_response(_make_body(value=[s1])))
        api = self._make_api()
        sheets = await api.list_sheets_async()
        self.assertEqual(len(sheets), 1)
        self.assertEqual(sheets[0]["name"], "表1")

    @patch("dingtalk_ai_table.get_access_token_async", new_callable=AsyncMock)
    @patch("dingtalk_ai_table.notable_client.Client")
    async def test_delete_sheet_async(self, MockNotable, _mock_token):
        _mock_token.return_value = "tok"
        mock_cli = MockNotable.return_value
        mock_cli.delete_sheet_with_options_async = AsyncMock(return_value=_make_response(_make_body(success=True)))
        api = self._make_api()
        ok = await api.delete_sheet_async("s1")
        self.assertTrue(ok)


# ---------------------------------------------------------------------------
# 16.5 查重新增（insert_records_if_not_exists）测试
# ---------------------------------------------------------------------------

class TestInsertIfNotExists(unittest.TestCase):
    """查重后仅新增：按全部列/指定列去重、批内去重、多选值规范化。"""

    def _make_api(self):
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
        }):
            return DingTalkAITable(operator_id="op_001")

    def _patch_existing(self, api, existing_fields):
        """把表中已有记录 mock 成给定 fields 列表。"""
        existing = [{"id": "old_%d" % i, "fields": f} for i, f in enumerate(existing_fields)]
        api.list_all_records = MagicMock(return_value=existing)
        inserted = []

        def fake_insert(records, operator_id=None, client_token=None):
            ids = ["new_%d" % (len(inserted) + j) for j in range(len(records))]
            inserted.extend(records)
            return ids
        api.insert_records = MagicMock(side_effect=fake_insert)
        return inserted

    # ---- 指纹辅助函数 ----

    def test_fingerprint_same_all_fields(self):
        """内容相同（键顺序不同）指纹一致"""
        a = {"a": 1, "b": 2}
        b = {"b": 2, "a": 1}
        self.assertEqual(m._make_dedup_fingerprint(a), m._make_dedup_fingerprint(b))

    def test_fingerprint_different_all_fields(self):
        """任一字段不同指纹不同"""
        self.assertNotEqual(
            m._make_dedup_fingerprint({"a": 1}),
            m._make_dedup_fingerprint({"a": 2}),
        )

    def test_fingerprint_designated_fields_only(self):
        """指定列查重时，非查重列的差异不影响指纹"""
        a = {"编号": "A001", "备注": "x"}
        b = {"编号": "A001", "备注": "y"}
        fp_a = m._make_dedup_fingerprint(a, ["编号"])
        fp_b = m._make_dedup_fingerprint(b, ["编号"])
        self.assertEqual(fp_a, fp_b)
        # 全列查重时应不同
        self.assertNotEqual(m._make_dedup_fingerprint(a), m._make_dedup_fingerprint(b))

    def test_fingerprint_multi_fields_combination(self):
        """多字段组合：单字段相同但组合不同 → 指纹不同"""
        a = {"部门": "研发", "姓名": "张三"}
        b = {"部门": "销售", "姓名": "张三"}
        self.assertNotEqual(
            m._make_dedup_fingerprint(a, ["部门", "姓名"]),
            m._make_dedup_fingerprint(b, ["部门", "姓名"]),
        )
        c = {"部门": "研发", "姓名": "张三"}
        self.assertEqual(
            m._make_dedup_fingerprint(a, ["部门", "姓名"]),
            m._make_dedup_fingerprint(c, ["部门", "姓名"]),
        )

    def test_fingerprint_missing_field_as_none(self):
        """指定列缺失时按 None 处理，与显式 None 等价"""
        fp_missing = m._make_dedup_fingerprint({"a": 1}, ["a", "b"])
        fp_none = m._make_dedup_fingerprint({"a": 1, "b": None}, ["a", "b"])
        self.assertEqual(fp_missing, fp_none)

    def test_normalize_multiselect_write_vs_read(self):
        """多选：写入字符串数组 与 读回对象数组 规范化后一致"""
        write_val = ["标签A", "标签B"]
        read_val = [{"name": "标签B", "id": "idB"}, {"name": "标签A", "id": "idA"}]
        self.assertEqual(
            m._normalize_dedup_value(write_val),
            m._normalize_dedup_value(read_val),
        )

    def test_number_int_vs_numeric_string(self):
        """number：写入int 与 读回数字字符串 指纹一致"""
        write_rec = {"数量": 10}
        read_rec = {"数量": "10"}
        self.assertEqual(
            m._make_dedup_fingerprint(write_rec),
            m._make_dedup_fingerprint(read_rec),
        )

    def test_empty_list_equals_missing_key(self):
        """空多选[]读回时键缺失：两者指纹一致"""
        write_rec = {"标题": "B", "标签": []}
        read_rec = {"标题": "B"}  # 读回时空多选字段不返回
        self.assertEqual(
            m._make_dedup_fingerprint(write_rec),
            m._make_dedup_fingerprint(read_rec),
        )

    def test_empty_string_equals_missing_key(self):
        """空字符串与缺失键指纹一致"""
        self.assertEqual(
            m._make_dedup_fingerprint({"a": "x", "b": ""}),
            m._make_dedup_fingerprint({"a": "x"}),
        )

    def test_float_integer_normalized(self):
        """10.0 与 10 与 '10' 指纹一致"""
        self.assertEqual(
            m._make_dedup_fingerprint({"n": 10.0}),
            m._make_dedup_fingerprint({"n": 10}),
        )
        self.assertEqual(
            m._make_dedup_fingerprint({"n": 10.0}),
            m._make_dedup_fingerprint({"n": "10"}),
        )

    # ---- 主流程 ----

    def test_all_new_when_table_empty(self):
        """空表：全部新增"""
        api = self._make_api()
        inserted = self._patch_existing(api, [])
        result = api.insert_records_if_not_exists([{"a": 1}, {"a": 2}])
        self.assertEqual(result["inserted_count"], 2)
        self.assertEqual(result["skipped_count"], 0)
        self.assertEqual(result["skipped_indexes"], [])
        self.assertEqual(len(inserted), 2)

    def test_skip_existing_by_all_fields(self):
        """全列查重：已存在的跳过，新的新增"""
        api = self._make_api()
        inserted = self._patch_existing(api, [{"a": 1, "b": 2}])
        result = api.insert_records_if_not_exists([
            {"a": 1, "b": 2},   # 已存在 → 跳过（下标0）
            {"a": 3, "b": 4},   # 新增
        ])
        self.assertEqual(result["inserted_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["skipped_indexes"], [0])
        self.assertEqual(inserted, [{"a": 3, "b": 4}])

    def test_dedup_by_single_field(self):
        """按单个字段查重：该字段相同即跳过，忽略其他字段差异"""
        api = self._make_api()
        inserted = self._patch_existing(api, [{"编号": "A001", "备注": "旧"}])
        result = api.insert_records_if_not_exists(
            [{"编号": "A001", "备注": "新"}, {"编号": "A002", "备注": "x"}],
            dedup_fields=["编号"],
        )
        self.assertEqual(result["inserted_count"], 1)
        self.assertEqual(result["skipped_indexes"], [0])
        self.assertEqual(inserted[0]["编号"], "A002")

    def test_intra_batch_duplicate_removed(self):
        """同一批次内部的重复记录只新增一次"""
        api = self._make_api()
        inserted = self._patch_existing(api, [])
        result = api.insert_records_if_not_exists([
            {"a": 1}, {"a": 1}, {"a": 2},
        ])
        self.assertEqual(result["inserted_count"], 2)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["skipped_indexes"], [1])

    def test_multiselect_existing_detected(self):
        """多选字段：读回对象数组，写入字符串数组也能识别为已存在"""
        api = self._make_api()
        inserted = self._patch_existing(api, [{
            "标题": "x",
            "标签": [{"name": "A", "id": "i1"}, {"name": "B", "id": "i2"}],
        }])
        result = api.insert_records_if_not_exists([
            {"标题": "x", "标签": ["B", "A"]},  # 同一记录（顺序不同、格式不同）→ 跳过
            {"标题": "y", "标签": ["C"]},
        ])
        self.assertEqual(result["inserted_count"], 1)
        self.assertEqual(result["skipped_indexes"], [0])

    def test_empty_input_returns_zero(self):
        """空输入不调用查询和新增，直接返回0"""
        api = self._make_api()
        api.list_all_records = MagicMock()
        api.insert_records = MagicMock()
        result = api.insert_records_if_not_exists([])
        self.assertEqual(result["inserted_count"], 0)
        self.assertEqual(result["skipped_count"], 0)
        api.list_all_records.assert_not_called()
        api.insert_records.assert_not_called()

    def test_all_exist_nothing_inserted(self):
        """全部已存在时不调用 insert_records"""
        api = self._make_api()
        inserted = self._patch_existing(api, [{"a": 1}, {"a": 2}])
        api.insert_records = MagicMock(return_value=[])
        result = api.insert_records_if_not_exists([{"a": 1}, {"a": 2}])
        self.assertEqual(result["inserted_count"], 0)
        self.assertEqual(result["skipped_count"], 2)
        api.insert_records.assert_not_called()


class TestInsertIfNotExistsAsync(unittest.IsolatedAsyncioTestCase):
    """查重新增异步版本测试。"""

    def _make_api(self):
        with patch.object(m, "load_config", return_value={
            "client_id": "cid", "client_secret": "cs",
            "document_id": "base1", "table_name": "sheet1",
        }):
            return DingTalkAITable(operator_id="op_001")

    async def test_async_dedup_flow(self):
        """异步：已存在跳过、新增不存在"""
        api = self._make_api()
        existing = [{"id": "old_0", "fields": {"k": "v1"}}]
        api.list_all_records_async = AsyncMock(return_value=existing)

        async def fake_insert(records, operator_id=None, client_token=None):
            return ["new_%d" % j for j in range(len(records))]
        api.insert_records_async = AsyncMock(side_effect=fake_insert)

        result = await api.insert_records_if_not_exists_async([
            {"k": "v1"},  # 已存在
            {"k": "v2"},  # 新增
        ])
        self.assertEqual(result["inserted_count"], 1)
        self.assertEqual(result["skipped_count"], 1)
        self.assertEqual(result["skipped_indexes"], [0])

    async def test_async_empty_input(self):
        """异步空输入"""
        api = self._make_api()
        api.list_all_records_async = AsyncMock()
        result = await api.insert_records_if_not_exists_async([])
        self.assertEqual(result["inserted_count"], 0)
        api.list_all_records_async.assert_not_called()


# ---------------------------------------------------------------------------
# 17. 集成测试（默认跳过，设置 RUN_INTEGRATION=1 时执行）
# ---------------------------------------------------------------------------

@unittest.skipUnless(
    os.environ.get("RUN_INTEGRATION") == "1",
    "跳过集成测试，设置 RUN_INTEGRATION=1 执行真实API联调",
)
class TestIntegration(unittest.TestCase):
    """真实API联调，需要 .config 配置正确且设置 OPERATOR_ID 环境变量。"""

    def setUp(self):
        self.operator_id = os.environ.get("OPERATOR_ID")
        if not self.operator_id:
            self.skipTest("未设置 OPERATOR_ID 环境变量")
        self.api = DingTalkAITable(operator_id=self.operator_id)
        self.created_ids = []

    def tearDown(self):
        if self.created_ids:
            try:
                self.api.delete_records(self.created_ids)
            except Exception:
                pass

    def test_full_crud_flow(self):
        """完整增→查→改→删流程"""
        # 增
        ids = self.api.insert_records([{"标题": "集成测试", "数字": 1}])
        self.assertEqual(len(ids), 1)
        self.created_ids.extend(ids)

        # 查
        rec = self.api.get_record(ids[0])
        self.assertEqual(rec["id"], ids[0])

        # 改
        updated = self.api.update_records([{"id": ids[0], "fields": {"数字": 2}}])
        self.assertEqual(updated, ids)

        # 删
        ok = self.api.delete_records(ids)
        self.assertTrue(ok)
        self.created_ids.clear()


if __name__ == "__main__":
    unittest.main(verbosity=2)
