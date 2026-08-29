"""后端功能综合测试（四层记忆模块 + 会话/角色卡库 + API）。

覆盖范围：
- storage 层：memory 结构规范化、会话 CRUD、各层读写、角色卡库、vm 迁移
- memory_engine 层：四层上下文注入、事实抽取与去重、剧情总结增量幂等、自动触发阈值
- vector_memory 层：top_k 语义（None/0/N）、build_history 拼接与 _src 标记
- app 层：注册/登录、四层记忆 API、角色卡库 API、流式 chat 收尾记忆维护

测试策略：
- 所有 LLM 调用均 mock（llm_client.chat 打桩），不调用真实 API。
- 向量层用确定性伪向量替换 encode，不加载真实 embedding 模型。
- 使用临时数据目录，不污染真实 data/app.db。

运行：python tests/test_backend.py
"""

import hashlib
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

# 确保能导入项目根目录的模块（config / storage / app ...）
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ---------- 环境准备：临时数据目录（必须在导入业务模块前设置） ----------
TMP_DIR = tempfile.mkdtemp(prefix="dpsklite_test_")

import config  # noqa: E402

config.VECTOR_MEMORY_DB = os.path.join(TMP_DIR, "vector_memory.db")

import storage  # noqa: E402

storage.DATA_DIR = TMP_DIR
storage.SESSIONS_DIR = os.path.join(TMP_DIR, "sessions")
storage.PRESETS_FILE = os.path.join(TMP_DIR, "presets.json")
storage.DB_FILE = os.path.join(TMP_DIR, "app.db")
storage._init_db()  # 重建数据库到临时库

import llm_client  # noqa: E402
import memory_engine  # noqa: E402
import vector_memory  # noqa: E402

import app as app_module  # noqa: E402


# ============================== storage 层 ==============================

class TestStorageParse(unittest.TestCase):
    """memory 结构规范化。"""

    def test_parse_vm_default(self):
        v = storage._parse_vm(None)
        self.assertFalse(v["enabled"])
        self.assertIsNone(v["top_k"])  # None = 用默认
        self.assertEqual(v["recent_n"], config.VECTOR_MEMORY_RECENT_N)

    def test_parse_vm_top_k_zero_kept(self):
        """top_k=0（自动召回）不能被吞成 None。"""
        v = storage._parse_vm({"top_k": 0})
        self.assertEqual(v["top_k"], 0)

    def test_parse_vm_recent_n_zero_kept(self):
        """recent_n=0（全量模式）不能被吞成默认值。"""
        v = storage._parse_vm({"recent_n": 0})
        self.assertEqual(v["recent_n"], 0)

    def test_parse_vm_top_k_empty_is_none(self):
        v = storage._parse_vm({"top_k": ""})
        self.assertIsNone(v["top_k"])

    def test_parse_vm_clamp(self):
        v = storage._parse_vm({"recent_n": 99999, "top_k": 99999})
        self.assertEqual(v["recent_n"], 1000)
        self.assertEqual(v["top_k"], 500)

    def test_parse_vm_string_input(self):
        v = storage._parse_vm('{"enabled": true, "recent_n": 6}')
        self.assertTrue(v["enabled"])
        self.assertEqual(v["recent_n"], 6)

    def test_parse_memory_normalize(self):
        raw = {
            "card": {"content": "卡", "source": "paste"},
            "facts": [{"text": "事实1"}, {"text": "  "}, {"text": 123}],
            "summary": {"text": "摘要", "last_round": "5", "slice_rounds": 3, "auto_rounds": 0},
            "vector": {"top_k": 0},
        }
        mem = storage._parse_memory(raw)
        self.assertEqual(mem["card"]["content"], "卡")
        # 空串被过滤；int 文本规范化为 str
        self.assertEqual([f["text"] for f in mem["facts"]], ["事实1", "123"])
        self.assertEqual(mem["summary"]["last_round"], 5)
        self.assertEqual(mem["summary"]["slice_rounds"], 3)
        self.assertEqual(mem["summary"]["auto_rounds"], 0)
        self.assertEqual(mem["vector"]["top_k"], 0)

    def test_parse_memory_bad_input(self):
        mem = storage._parse_memory("not-json")
        self.assertIsNone(mem["card"])
        self.assertEqual(mem["facts"], [])
        mem2 = storage._parse_memory(None)
        self.assertIsNone(mem2["summary"])

    def test_parse_summary_clamp(self):
        s = storage._parse_summary({"slice_rounds": 0, "auto_rounds": -5})
        self.assertEqual(s["slice_rounds"], 1)
        self.assertEqual(s["auto_rounds"], 0)
        s = storage._parse_summary({"slice_rounds": 999, "auto_rounds": 99999})
        self.assertEqual(s["slice_rounds"], 200)
        self.assertEqual(s["auto_rounds"], 1000)

    def test_parse_summary_defaults_from_config(self):
        s = storage._parse_summary(None)
        self.assertEqual(s["slice_rounds"], config.SUMMARY_SLICE_ROUNDS)
        self.assertEqual(s["auto_rounds"], config.SUMMARY_AUTO_ROUNDS)


class TestStorageCRUD(unittest.TestCase):
    """会话 + 四层记忆 + 角色卡库读写。"""

    def setUp(self):
        self.username = f"crud{int(time.time() * 1000)}"
        storage.create_user(self.username, "test123456")
        self.sid = storage.create_session(self.username)["id"]

    def test_session_default_memory(self):
        mem = storage.get_session_memory(self.username, self.sid)
        self.assertIsNone(mem["card"])
        self.assertEqual(mem["facts"], [])
        self.assertIsNone(mem["summary"])
        self.assertFalse(mem["vector"]["enabled"])
        # 兼容旧前端：vm 从 memory.vector 权威读取（值一致）
        session = storage.get_session(self.username, self.sid)
        self.assertEqual(session["vm"], mem["vector"])

    def test_card_rw(self):
        mem = storage.set_session_card(self.username, self.sid, "你叫小明", source="paste")
        self.assertEqual(mem["card"]["content"], "你叫小明")
        self.assertEqual(mem["card"]["source"], "paste")
        self.assertIn("updated_at", mem["card"])
        # 清空
        mem = storage.set_session_card(self.username, self.sid, "")
        self.assertIsNone(mem["card"])

    def test_facts_rw(self):
        mem = storage.set_session_facts(self.username, self.sid, [{"text": "A"}, {"text": "B"}, {"text": "  "}])
        self.assertEqual([f["text"] for f in mem["facts"]], ["A", "B"])
        # 整体覆盖
        mem = storage.set_session_facts(self.username, self.sid, [{"text": "C"}])
        self.assertEqual([f["text"] for f in mem["facts"]], ["C"])

    def test_summary_keeps_config(self):
        storage.set_session_summary_config(self.username, self.sid, slice_rounds=4, auto_rounds=6)
        mem = storage.set_session_summary(self.username, self.sid, "摘要文本", last_round=10)
        s = mem["summary"]
        self.assertEqual(s["text"], "摘要文本")
        self.assertEqual(s["last_round"], 10)
        self.assertEqual(s["slice_rounds"], 4)  # 用户配置保留
        self.assertEqual(s["auto_rounds"], 6)

    def test_summary_config_partial_update(self):
        mem = storage.set_session_summary_config(self.username, self.sid, slice_rounds=4)
        self.assertEqual(mem["summary"]["slice_rounds"], 4)
        self.assertEqual(mem["summary"]["auto_rounds"], config.SUMMARY_AUTO_ROUNDS)  # 未动
        mem = storage.set_session_summary_config(self.username, self.sid, auto_rounds=0)
        self.assertEqual(mem["summary"]["auto_rounds"], 0)

    def test_memory_switches_one_click(self):
        """一键配置：打开 2/3/4 开关并恢复数值默认（最近 N=10、摘要切片/自动、向量 TopK/recent_n）。"""
        storage.set_session_facts(self.username, self.sid, [{"text": "主角叫艾伦"}])
        storage.set_session_summary(self.username, self.sid, "艾伦出发去冒险。", last_round=2)
        storage.set_session_vector_config(self.username, self.sid, recent_n=0, enabled=False, top_k=0)
        mem = storage.set_session_memory_switches(
            self.username, self.sid,
            facts_enabled=True, summary_enabled=True, vector_enabled=True, reset_values=True,
        )
        self.assertTrue(mem["facts_enabled"])
        self.assertTrue(mem["summary"]["enabled"])
        self.assertTrue(mem["vector"]["enabled"])
        self.assertEqual(mem["recent_n"], config.VECTOR_MEMORY_RECENT_N)  # 10
        self.assertEqual(mem["summary"]["slice_rounds"], config.SUMMARY_SLICE_ROUNDS)
        self.assertEqual(mem["summary"]["auto_rounds"], config.SUMMARY_AUTO_ROUNDS)
        self.assertIsNone(mem["vector"]["top_k"])  # 恢复默认召回
        self.assertEqual(mem["vector"]["recent_n"], config.VECTOR_MEMORY_RECENT_N)
        # 内容保留（开关不删内容）
        self.assertEqual([f["text"] for f in mem["facts"]], ["主角叫艾伦"])
        self.assertEqual(mem["summary"]["text"], "艾伦出发去冒险。")

    def test_memory_switches_disable_keeps_content(self):
        """关闭智能总结：关闭 2/3/4 开关，但内容保留、0/1 不受影响。"""
        storage.set_session_facts(self.username, self.sid, [{"text": "主角叫艾伦"}])
        storage.set_session_summary(self.username, self.sid, "艾伦出发去冒险。", last_round=2)
        storage.set_session_vector_config(self.username, self.sid, enabled=True, top_k=5)
        mem = storage.set_session_memory_switches(
            self.username, self.sid,
            facts_enabled=False, summary_enabled=False, vector_enabled=False,
        )
        self.assertFalse(mem["facts_enabled"])
        self.assertFalse(mem["summary"]["enabled"])
        self.assertFalse(mem["vector"]["enabled"])
        # 内容保留
        self.assertEqual([f["text"] for f in mem["facts"]], ["主角叫艾伦"])
        self.assertEqual(mem["summary"]["text"], "艾伦出发去冒险。")
        self.assertEqual(mem["vector"]["top_k"], 5)

    def test_vector_config_top_k_zero(self):
        mem = storage.set_session_vector_config(self.username, self.sid, top_k=0, enabled=True)
        self.assertEqual(mem["vector"]["top_k"], 0)  # 0 不被吞
        self.assertTrue(mem["vector"]["enabled"])

    def test_vector_config_recent_n_zero_kept(self):
        """recent_n=0（全量模式）保存时不能被钳成 1。"""
        mem = storage.set_session_vector_config(self.username, self.sid, recent_n=0)
        self.assertEqual(mem["vector"]["recent_n"], 0)

    def test_vector_config_top_k_none_clears(self):
        """显式传 None（前端清空输入框）应清除 top_k 恢复默认，而非保留旧值。"""
        storage.set_session_vector_config(self.username, self.sid, top_k=12)
        mem = storage.set_session_vector_config(self.username, self.sid, top_k=None)
        self.assertIsNone(mem["vector"]["top_k"])

    def test_vector_config_top_k_unset_keeps(self):
        """未传 top_k（哨兵 _UNSET）不更新该字段。"""
        storage.set_session_vector_config(self.username, self.sid, top_k=7)
        mem = storage.set_session_vector_config(self.username, self.sid, recent_n=9)
        self.assertEqual(mem["vector"]["top_k"], 7)  # 保留
        self.assertEqual(mem["vector"]["recent_n"], 9)

    def test_vector_config_partial(self):
        mem = storage.set_session_vector_config(self.username, self.sid, recent_n=8, model="Qwen")
        self.assertEqual(mem["vector"]["recent_n"], 8)
        self.assertEqual(mem["vector"]["model"], "Qwen")

    def test_clear_layer(self):
        storage.set_session_card(self.username, self.sid, "卡")
        storage.set_session_facts(self.username, self.sid, [{"text": "A"}])
        storage.set_session_summary(self.username, self.sid, "摘要", last_round=1)
        mem = storage.clear_session_memory_layer(self.username, self.sid, "card")
        self.assertIsNone(mem["card"])
        mem = storage.clear_session_memory_layer(self.username, self.sid, "facts")
        self.assertEqual(mem["facts"], [])
        mem = storage.clear_session_memory_layer(self.username, self.sid, "summary")
        self.assertIsNone(mem["summary"])

    def test_save_session_memory_roundtrip(self):
        mem = storage.get_session_memory(self.username, self.sid)
        mem["card"] = {"content": "持久化卡", "source": "file", "updated_at": time.time()}
        storage.save_session_memory(self.username, self.sid, mem)
        got = storage.get_session_memory(self.username, self.sid)
        self.assertEqual(got["card"]["content"], "持久化卡")

    def test_character_cards(self):
        card = storage.save_character_card("测试卡", "内容")
        self.assertIsNotNone(card["id"])
        cards = storage.list_character_cards(self.username)
        self.assertEqual(len(cards), 1)
        got = storage.get_character_card(card["id"])
        self.assertEqual(got["content"], "内容")
        # 更新
        storage.save_character_card("新名", "新内容", card_id=card["id"])
        got = storage.get_character_card(card["id"])
        self.assertEqual(got["name"], "新名")
        self.assertEqual(got["content"], "新内容")
        # 删除
        storage.delete_character_card(card["id"])
        self.assertIsNone(storage.get_character_card(card["id"]))

    def test_missing_session_returns_none(self):
        self.assertIsNone(storage.get_session_memory(self.username, "no-such-sid"))
        self.assertIsNone(storage.set_session_card(self.username, "no-such-sid", "x"))


# ============================== memory_engine 层 ==============================

class TestMemoryEngine(unittest.TestCase):
    """四层上下文注入 + 事实抽取/去重 + 剧情总结增量幂等。"""

    def setUp(self):
        self.username = f"meme{int(time.time() * 1000)}"
        storage.create_user(self.username, "test123456")
        self.sid = storage.create_session(self.username)["id"]

    @staticmethod
    def _msgs(n=4):
        out = []
        for i in range(n):
            out.append({"role": "user", "content": f"提问{i}"})
            out.append({"role": "assistant", "content": f"回答{i}"})
        return out

    def test_build_context_three_unconditional_layers(self):
        """①②③ 无条件注入且在前（system role），最近窗口在后。"""
        storage.set_session_card(self.username, self.sid, "你是勇敢的骑士。", source="paste")
        storage.set_session_facts(self.username, self.sid, [{"text": "主角叫艾伦"}])
        storage.set_session_summary(self.username, self.sid, "艾伦出发去冒险。", last_round=2)
        ctx = memory_engine.build_context(self.username, self.sid, self._msgs(3))
        self.assertEqual([m["role"] for m in ctx[:3]], ["system", "system", "system"])
        contents = [m["content"] for m in ctx]
        self.assertTrue(any("骑士" in c for c in contents))
        self.assertTrue(any("关键事实" in c for c in contents))
        self.assertTrue(any("剧情摘要" in c for c in contents))
        # 最近窗口（对话层）在最后
        self.assertEqual(ctx[-1], {"role": "assistant", "content": "回答2"})

    def test_build_context_empty_memory(self):
        """无记忆时只有最近窗口。"""
        ctx = memory_engine.build_context(self.username, self.sid, self._msgs(2))
        self.assertEqual(len(ctx), 4)
        self.assertTrue(all(m["role"] in ("user", "assistant") for m in ctx))

    def test_build_context_no_session_fallback(self):
        ctx = memory_engine.build_context(None, None, self._msgs(3))
        self.assertEqual(len(ctx), 6)

    def test_build_context_vector_layer(self):
        """④ 向量层启用：检索片段在前、最近窗口在后。"""
        storage.set_session_vector_config(self.username, self.sid, enabled=True, top_k=5)
        fake_vm = mock.MagicMock()
        fake_vm.build_history.return_value = [
            {"role": "user", "content": "早期提问", "_src": "vector"},
            {"role": "assistant", "content": "早期回答", "_src": "vector"},
        ]
        with mock.patch("vector_memory.get_instance", return_value=fake_vm):
            ctx = memory_engine.build_context(
                self.username, self.sid, self._msgs(3), data={"model": "deepseek-chat"}
            )
        self.assertTrue(any(m["content"] == "早期提问" for m in ctx))
        fake_vm.sync_session.assert_called()  # 未传 stored_msgs -> sync
        fake_vm.build_history.assert_called()
        # top_k 按会话配置 5 传递
        _, kwargs = fake_vm.build_history.call_args
        self.assertEqual(kwargs["top_k"], 5)

    def test_build_context_n_zero_full_window_keeps_vector_and_summary(self):
        """N=0 全量模式：不再绕过向量与摘要——向量照常召回、摘要照常注入。"""
        storage.set_session_card(self.username, self.sid, "你是勇敢的骑士。", source="paste")
        storage.set_session_facts(self.username, self.sid, [{"text": "主角叫艾伦"}])
        storage.set_session_summary(self.username, self.sid, "艾伦出发去冒险。", last_round=2)
        storage.set_session_vector_config(self.username, self.sid, recent_n=0, enabled=True, top_k=5)
        fake_vm = mock.MagicMock()
        fake_vm.build_history.return_value = [
            {"role": "user", "content": "早期提问", "_src": "vector"},
            {"role": "assistant", "content": "早期回答", "_src": "vector"},
        ]
        with mock.patch("vector_memory.get_instance", return_value=fake_vm):
            ctx = memory_engine.build_context(
                self.username, self.sid, self._msgs(3), data={"model": "deepseek-chat"}
            )
        fake_vm.build_history.assert_called()  # N=0 照常向量召回
        # N=0：最近窗口尽量塞满（recent_rounds 极大，内部钳制到全部轮次）
        _, kwargs = fake_vm.build_history.call_args
        self.assertGreaterEqual(kwargs["recent_rounds"], 10 ** 6)
        contents = [m["content"] for m in ctx]
        self.assertTrue(any("骑士" in c for c in contents))      # ① 卡照常
        self.assertTrue(any("关键事实" in c for c in contents))  # ② 事实照常
        self.assertTrue(any("剧情摘要" in c for c in contents))  # ③ 摘要照常注入
        self.assertTrue(any("早期提问" in c for c in contents))  # ④ 向量片段在前
        # 已总结文本保留在库
        mem = storage.get_session_memory(self.username, self.sid)
        self.assertEqual(mem["summary"]["text"], "艾伦出发去冒险。")

    def test_should_auto_summary_enabled_when_n_zero(self):
        """N=0 全量模式：自动总结照常触发（N 不再影响摘要开关）。"""
        storage.set_session_summary(self.username, self.sid, "摘要", last_round=1)
        for i in range(30):
            storage.append_message(self.username, self.sid, "user", f"q{i}")
            storage.append_message(self.username, self.sid, "assistant", f"a{i}")
        storage.set_session_vector_config(self.username, self.sid, recent_n=0)
        self.assertTrue(memory_engine.should_auto_summary(self.username, self.sid))

    def test_should_auto_summary_disabled_when_switch_off(self):
        """剧情摘要开关关闭：自动总结不触发（与 N 无关）。"""
        storage.set_session_summary(self.username, self.sid, "摘要", last_round=1)
        for i in range(30):
            storage.append_message(self.username, self.sid, "user", f"q{i}")
            storage.append_message(self.username, self.sid, "assistant", f"a{i}")
        storage.set_session_memory_switches(self.username, self.sid, summary_enabled=False)
        self.assertFalse(memory_engine.should_auto_summary(self.username, self.sid))

    def test_extract_facts_merge_and_dedup(self):
        """LLM 返回新事实：合并旧事实 + 相似去重。"""
        old = [{"text": "主角叫小明", "ts": time.time()}]
        with mock.patch(
            "llm_client.chat",
            return_value="- 主角叫小明\n- 小明喜欢小美\n- 主角叫小明（又名阿明）",
        ):
            new = memory_engine.extract_facts(
                "fake-key", old, [{"role": "user", "content": "我叫小明，我喜欢小美"}]
            )
        texts = [f["text"] for f in new]
        self.assertIn("主角叫小明", texts)
        self.assertIn("小明喜欢小美", texts)
        # 相似重复被合并（旧事实 ts 保留）
        self.assertEqual(len(texts), 2)
        self.assertEqual(new[0]["ts"], old[0]["ts"])

    def test_extract_facts_failure_graceful(self):
        """LLM 失败时返回旧事实（优雅降级）。"""
        old = [{"text": "旧事实", "ts": time.time()}]
        with mock.patch("llm_client.chat", side_effect=RuntimeError("API down")):
            new = memory_engine.extract_facts("fake-key", old, [{"role": "user", "content": "x"}])
        self.assertEqual(new, old)

    def test_extract_facts_no_new_text(self):
        old = [{"text": "旧事实", "ts": time.time()}]
        self.assertEqual(
            memory_engine.extract_facts("fake-key", old, [{"role": "user", "content": "  "}]),
            old,
        )

    def test_dedup_facts_exact_and_similar(self):
        out = memory_engine._dedup_facts([
            "主角叫小明",
            "主角叫小明",                  # 精确重复
            "主角叫小明，大家都叫他阿明",   # 相似（被覆盖）
            "小美是女主角",
        ])
        self.assertEqual(len(out), 2)
        self.assertIn("主角叫小明", out)
        self.assertIn("小美是女主角", out)

    def test_fact_similar(self):
        self.assertGreaterEqual(
            memory_engine._fact_similar("主角叫小明", "主角叫小明（又名阿明）"),
            memory_engine._FACT_SIM_THRESHOLD,
        )
        self.assertLess(
            memory_engine._fact_similar("主角叫小明", "今天下雨了"),
            memory_engine._FACT_SIM_THRESHOLD,
        )

    def test_run_summary_incremental(self):
        chat = self._msgs(6)
        # 无旧摘要时：6 轮 < 默认切片宽度 8 -> 1 个切片 -> summarize_slice 直接产出摘要
        with mock.patch("llm_client.chat", return_value="一段剧情摘要"):
            r = memory_engine.run_summary("fake-key", self.username, self.sid, chat)
        self.assertTrue(r["ok"])
        self.assertEqual(r["summary"], "一段剧情摘要")
        s = storage.get_session_memory(self.username, self.sid)["summary"]
        self.assertEqual(s["last_round"], 6)

    def test_run_summary_merge_with_old(self):
        """有旧摘要时走合并分支（merge_summary）。"""
        storage.set_session_summary(self.username, self.sid, "旧摘要", last_round=2)
        chat = self._msgs(6)  # 前 2 轮已总结，新增 4 轮
        with mock.patch(
            "llm_client.chat",
            side_effect=["新切片摘要", "合并后的完整摘要"],
        ):
            r = memory_engine.run_summary("fake-key", self.username, self.sid, chat)
        self.assertTrue(r["ok"])
        self.assertEqual(r["summary"], "合并后的完整摘要")
        # last_round 推进到全部 6 轮
        s = storage.get_session_memory(self.username, self.sid)["summary"]
        self.assertEqual(s["last_round"], 6)

    def test_run_summary_full_regenerates(self):
        """full=True（重新总结）：忽略旧摘要与总结点，从全部历史重新生成并覆盖。"""
        storage.set_session_summary(self.username, self.sid, "旧摘要", last_round=2)
        chat = self._msgs(6)
        # 断言 LLM 输入里不出现旧摘要（未走合并分支，而是重新切片总结）
        def fake_chat(api_key, model, messages, **kwargs):
            text = " ".join(m.get("content", "") for m in messages)
            self.assertNotIn("旧摘要", text)
            return "全新摘要"
        with mock.patch("llm_client.chat", side_effect=fake_chat):
            r = memory_engine.run_summary("fake-key", self.username, self.sid, chat, full=True)
        self.assertTrue(r["ok"])
        self.assertEqual(r["summary"], "全新摘要")
        s = storage.get_session_memory(self.username, self.sid)["summary"]
        self.assertEqual(s["text"], "全新摘要")  # 覆盖旧摘要
        self.assertEqual(s["last_round"], 6)

    def test_extract_facts_for_session_full_uses_all_history(self):
        """full=True：用全部历史重新抽取（而非只取最近片段）。"""
        for i in range(30):
            storage.append_message(self.username, self.sid, "user", f"q{i}")
            storage.append_message(self.username, self.sid, "assistant", f"a{i}")
        captured = {}
        def fake_chat(api_key, model, messages, **kwargs):
            captured["text"] = messages[-1]["content"]
            return "- 主角叫小明"
        with mock.patch("llm_client.chat", side_effect=fake_chat):
            new = memory_engine.extract_facts_for_session(
                "fake-key", self.username, self.sid, None, full=True
            )
        self.assertIsNotNone(new)
        self.assertIn("q0", captured["text"])   # 包含最早消息
        self.assertIn("q29", captured["text"])  # 也包含最新消息
        # 走纯抽取提示词（不携带旧事实，不是合并提示词）
        self.assertNotIn("已有事实", captured["text"])

    def test_extract_facts_for_session_full_replaces(self):
        """full=True（重新总结）：结果整体替换旧事实，而不是合并追加。"""
        storage.set_session_facts(self.username, self.sid, [{"text": "过时的旧事实"}])
        for i in range(4):
            storage.append_message(self.username, self.sid, "user", f"q{i}")
            storage.append_message(self.username, self.sid, "assistant", f"a{i}")
        with mock.patch("llm_client.chat", return_value="- 主角叫小明\n- 小美是女主角"):
            new = memory_engine.extract_facts_for_session(
                "fake-key", self.username, self.sid, None, full=True
            )
        texts = [f["text"] for f in new]
        self.assertEqual(texts, ["主角叫小明", "小美是女主角"])  # 旧事实被替换掉
        mem = storage.get_session_memory(self.username, self.sid)
        self.assertEqual([f["text"] for f in mem["facts"]], ["主角叫小明", "小美是女主角"])

    def test_extract_facts_for_session_incremental_merges(self):
        """full=False（默认）：增量抽取与旧事实合并，旧事实保留。"""
        storage.set_session_facts(self.username, self.sid, [{"text": "旧事实保留"}])
        for i in range(4):
            storage.append_message(self.username, self.sid, "user", f"q{i}")
            storage.append_message(self.username, self.sid, "assistant", f"a{i}")
        with mock.patch("llm_client.chat", return_value="- 新事实A"):
            new = memory_engine.extract_facts_for_session(
                "fake-key", self.username, self.sid, None
            )
        texts = [f["text"] for f in new]
        self.assertIn("旧事实保留", texts)  # 增量合并，旧事实保留
        self.assertIn("新事实A", texts)

    def test_run_summary_idempotent(self):
        """无新内容时返回旧摘要，不重复调用 LLM。"""
        chat = self._msgs(6)
        with mock.patch("llm_client.chat", side_effect=["合并后的完整摘要"] * 3):
            r1 = memory_engine.run_summary("fake-key", self.username, self.sid, chat)
        self.assertTrue(r1["ok"])
        with mock.patch("llm_client.chat") as m:
            r2 = memory_engine.run_summary("fake-key", self.username, self.sid, chat)
        self.assertTrue(r2["ok"])
        self.assertEqual(r2["summary"], "合并后的完整摘要")
        m.assert_not_called()

    def test_run_summary_no_chat(self):
        r = memory_engine.run_summary("fake-key", self.username, self.sid, [])
        self.assertFalse(r["ok"])

    def test_run_summary_missing_session(self):
        r = memory_engine.run_summary("fake-key", self.username, "no-such", self._msgs(2))
        self.assertFalse(r["ok"])

    def test_slice_chat(self):
        chat = self._msgs(9)  # 9 轮
        slices = memory_engine._slice_chat(chat, 4)
        self.assertEqual(len(slices), 3)  # 4 + 4 + 1
        self.assertEqual(len([m for m in slices[0] if m["role"] == "user"]), 4)
        self.assertEqual(len([m for m in slices[2] if m["role"] == "user"]), 1)

    def test_index_of_round(self):
        chat = self._msgs(5)
        self.assertEqual(memory_engine._index_of_round(chat, 2), 4)  # 2 轮 = 4 条
        self.assertEqual(memory_engine._index_of_round(chat, 99), len(chat))
        self.assertEqual(memory_engine._index_of_round(chat, 0), 0)

    def test_should_auto_summary(self):
        chat = self._msgs(5)
        # 默认阈值 10，5 轮不触发
        self.assertFalse(memory_engine.should_auto_summary(self.username, self.sid, chat))
        # 阈值调小 -> 触发
        storage.set_session_summary_config(self.username, self.sid, auto_rounds=5)
        self.assertTrue(memory_engine.should_auto_summary(self.username, self.sid, chat))
        # 已总结到 5 轮 -> 不再触发
        storage.set_session_summary(self.username, self.sid, "s", last_round=5)
        self.assertFalse(memory_engine.should_auto_summary(self.username, self.sid, chat))
        # auto_rounds=0 -> 关闭自动
        storage.set_session_summary_config(self.username, self.sid, auto_rounds=0)
        self.assertFalse(memory_engine.should_auto_summary(self.username, self.sid, self._msgs(20)))

    def test_extract_facts_for_session_saves(self):
        chat = self._msgs(4)
        with mock.patch("llm_client.chat", return_value="- 主角叫小明\n- 小明是骑士"):
            new = memory_engine.extract_facts_for_session("fake-key", self.username, self.sid, chat)
        self.assertIsNotNone(new)
        saved = storage.get_session_memory(self.username, self.sid)["facts"]
        self.assertEqual([f["text"] for f in saved], ["主角叫小明", "小明是骑士"])

    def test_extract_facts_for_session_no_change(self):
        """抽取结果与旧事实一致 -> 不写库、返回 None。"""
        storage.set_session_facts(self.username, self.sid, [{"text": "主角叫小明"}])
        chat = self._msgs(2)
        with mock.patch("llm_client.chat", return_value="- 主角叫小明"):
            new = memory_engine.extract_facts_for_session("fake-key", self.username, self.sid, chat)
        self.assertIsNone(new)

    def test_extract_facts_for_session_failure(self):
        with mock.patch("llm_client.chat", side_effect=RuntimeError("down")):
            new = memory_engine.extract_facts_for_session("fake-key", self.username, self.sid, self._msgs(2))
        self.assertIsNone(new)


# ============================== vector_memory 层 ==============================

def _fake_encode(texts, query=False, batch_size=16):
    """确定性伪向量：相同文本 -> 相同向量，不同文本 -> 不同向量。"""
    import numpy as np

    vecs = []
    for t in texts:
        h = int(hashlib.md5(str(t).encode("utf-8")).hexdigest(), 16)
        rng = np.random.RandomState(h & 0xFFFFFFFF)
        v = rng.rand(8)
        vecs.append(v)
    vecs = np.array(vecs, dtype=np.float32)
    norms = np.linalg.norm(vecs, axis=1, keepdims=True) + 1e-12
    return vecs / norms


class TestVectorMemory(unittest.TestCase):
    """向量检索 top_k 语义 + build_history 拼接（不加载真实模型）。"""

    def setUp(self):
        self.db = os.path.join(TMP_DIR, f"vm_test_{int(time.time() * 1000)}.db")
        self.vm = vector_memory.VectorMemory(db_path=self.db, lazy=True)
        self.vm.encode = _fake_encode

    def tearDown(self):
        self.vm.close()
        import gc

        gc.collect()  # 让 sqlite 连接句柄释放（Windows 上删除文件需要）
        try:
            if os.path.exists(self.db):
                os.remove(self.db)
        except OSError:
            pass  # 临时目录，删除失败可忽略

    def test_add_and_count(self):
        self.vm.add("s1", "user", "我叫小明，是一名骑士")
        self.vm.add("s1", "assistant", "你好，骑士小明")
        self.vm.add("s2", "user", "今天天气不错")
        self.assertEqual(self.vm.count("s1"), 2)
        self.assertEqual(self.vm.count("s2"), 1)

    def test_search_top_k_semantics(self):
        for i in range(10):
            self.vm.add("s1", "user", f"讨论话题{i}：关于魔法")
        # None -> 默认（5）
        self.assertEqual(len(self.vm.search("魔法", session_id="s1", top_k=None)), 5)
        # N -> 固定 N 条
        self.assertEqual(len(self.vm.search("魔法", session_id="s1", top_k=3)), 3)
        # 0 -> 自动召回全部
        self.assertEqual(len(self.vm.search("魔法", session_id="s1", top_k=0)), 10)
        # exclude_contents 排除
        self.assertEqual(
            len(self.vm.search("魔法", session_id="s1", top_k=0,
                               exclude_contents={"讨论话题0：关于魔法"})),
            9,
        )
        # 空查询返回 []
        self.assertEqual(self.vm.search("  ", session_id="s1"), [])

    def test_build_history_recent_and_vector(self):
        """向量命中展开为完整轮次（在前），最近窗口在后，_src 标记正确。"""
        chat = [
            {"role": "user", "content": "早期问题：什么是龙骑士"},
            {"role": "assistant", "content": "龙骑士是骑龙作战的勇士"},
            {"role": "user", "content": "现在我们讲到哪里了？"},
            {"role": "assistant", "content": "我们在讲龙骑士的起源。"},
            {"role": "user", "content": "好的继续"},
            {"role": "assistant", "content": "龙骑士起源于北方王国。"},
        ]
        fake_hits = [
            {"id": "h1", "session_id": "s1", "role": "user",
             "content": "早期问题：什么是龙骑士", "ts": 1.0, "score": 0.9},
        ]
        with mock.patch.object(self.vm, "search", return_value=fake_hits):
            history = self.vm.build_history(
                chat, session_id="s1", recent_rounds=2, top_k=5, min_score=0.0
            )
        srcs = [m.get("_src") for m in history]
        # 向量片段（早期轮次展开）在前
        self.assertEqual(srcs[:2], ["vector", "vector"])
        self.assertEqual([m["content"] for m in history[:2]],
                         ["早期问题：什么是龙骑士", "龙骑士是骑龙作战的勇士"])
        # 最近 2 轮（最后 4 条）在后
        self.assertEqual(srcs[2:], ["recent", "recent", "recent", "recent"])
        # 向量命中内容已在最近窗口内 -> 去重
        self.assertEqual(len(history), 6)

    def test_build_history_rounds_exceed_total(self):
        chat = [
            {"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "q2"}, {"role": "assistant", "content": "a2"},
        ]
        with mock.patch.object(self.vm, "search", return_value=[]):
            history = self.vm.build_history(chat, session_id="s1", recent_rounds=99, top_k=5, min_score=0.0)
        self.assertEqual(len(history), 4)  # 全部进入最近窗口


# ============================== app 层 API ==============================

class TestAPI(unittest.TestCase):
    """Flask test client 全流程 API 测试。"""

    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()
        cls.username = f"apiuser{int(time.time() * 1000)}"
        cls.password = "test123456"
        with cls.client.session_transaction() as sess:
            sess["captcha"] = "ABCD"
        r = cls.client.post("/api/auth/register", json={
            "username": cls.username, "password": cls.password, "captcha": "ABCD",
        })
        assert r.status_code == 200, r.get_json()

    def _new_session(self):
        r = self.client.post("/api/sessions", json={"title": "测试会话"})
        self.assertEqual(r.status_code, 200)
        return r.get_json()["id"]

    def test_login_and_protected(self):
        c2 = app_module.app.test_client()
        r = c2.get("/api/sessions")
        self.assertEqual(r.status_code, 401)  # 未登录
        with c2.session_transaction() as sess:
            sess["captcha"] = "WXYZ"
        r = c2.post("/api/auth/login", json={
            "username": self.username, "password": self.password, "captcha": "WXYZ",
        })
        self.assertEqual(r.status_code, 200)
        r = c2.get("/api/sessions")
        self.assertEqual(r.status_code, 200)
        # 错误验证码
        with c2.session_transaction() as sess:
            sess["captcha"] = "MNOP"
        r = c2.post("/api/auth/login", json={
            "username": self.username, "password": self.password, "captcha": "0000",
        })
        self.assertEqual(r.status_code, 400)

    def test_memory_api_roundtrip(self):
        sid = self._new_session()
        mem = {
            "card": {"content": "你是魔法师。", "source": "paste", "updated_at": time.time()},
            "facts": [{"text": "主角叫安娜", "ts": time.time()}],
            "summary": {"text": "安娜学习魔法。", "last_round": 2},
            "vector": {"enabled": False, "model": None, "recent_n": 10, "top_k": None},
        }
        r = self.client.post(f"/api/sessions/{sid}/memory", json={"memory": mem})
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/api/sessions/{sid}/memory")
        got = r.get_json()["memory"]
        self.assertEqual(got["card"]["content"], "你是魔法师。")
        self.assertEqual(got["facts"][0]["text"], "主角叫安娜")
        self.assertEqual(got["summary"]["last_round"], 2)

    def test_card_api(self):
        sid = self._new_session()
        r = self.client.post(f"/api/sessions/{sid}/card",
                             json={"content": "你是魔法师。", "source": "paste"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["memory"]["card"]["content"], "你是魔法师。")
        # 清空
        r = self.client.post(f"/api/sessions/{sid}/card", json={"content": ""})
        self.assertIsNone(r.get_json()["memory"]["card"])

    def test_card_lib_api(self):
        r = self.client.post("/api/cards", json={"name": "精灵公主", "content": "你是一位精灵公主"})
        self.assertEqual(r.status_code, 200)
        card = r.get_json()
        sid = self._new_session()
        r = self.client.post(f"/api/sessions/{sid}/card-lib", json={"card_id": card["id"]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["memory"]["card"]["source"], "card_lib")
        self.assertEqual(r.get_json()["memory"]["card"]["content"], "你是一位精灵公主")
        # 不存在的卡
        r = self.client.post(f"/api/sessions/{sid}/card-lib", json={"card_id": "nope"})
        self.assertEqual(r.status_code, 404)
        # 列表 + 删除
        r = self.client.get("/api/cards")
        self.assertTrue(any(c["id"] == card["id"] for c in r.get_json()["cards"]))
        r = self.client.delete(f"/api/cards/{card['id']}")
        self.assertEqual(r.status_code, 200)

    def test_facts_api(self):
        sid = self._new_session()
        r = self.client.post(f"/api/sessions/{sid}/facts",
                             json={"facts": [{"text": "主角叫安娜"}]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["memory"]["facts"][0]["text"], "主角叫安娜")
        # 非法 body
        r = self.client.post(f"/api/sessions/{sid}/facts", json={"facts": "oops"})
        self.assertEqual(r.status_code, 400)

    def test_summary_config_api(self):
        sid = self._new_session()
        r = self.client.post(f"/api/sessions/{sid}/summary-config",
                             json={"slice_rounds": 6, "auto_rounds": 12})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertEqual(body["slice_rounds"], 6)
        self.assertEqual(body["auto_rounds"], 12)
        r = self.client.get(f"/api/sessions/{sid}/summary-config")
        self.assertEqual(r.get_json()["auto_rounds"], 12)
        # 缺少参数
        r = self.client.post(f"/api/sessions/{sid}/summary-config", json={})
        self.assertEqual(r.status_code, 400)

    def test_vector_config_api(self):
        sid = self._new_session()
        r = self.client.post(f"/api/sessions/{sid}/vector-config",
                             json={"enabled": True, "top_k": 0, "recent_n": 8})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["enabled"])
        self.assertEqual(body["top_k"], 0)
        self.assertTrue(body["auto"])  # auto 召回
        # auto 快捷方式
        r = self.client.post(f"/api/sessions/{sid}/vector-config", json={"auto": True})
        self.assertTrue(r.get_json()["auto"])
        r = self.client.get(f"/api/sessions/{sid}/vector-config")
        self.assertEqual(r.get_json()["recent_n"], 8)

    def test_vector_config_recent_n_zero_api(self):
        """N=0（全量模式）通过 API 保存不被钳成 1。"""
        sid = self._new_session()
        r = self.client.post(f"/api/sessions/{sid}/vector-config", json={"recent_n": 0})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["recent_n"], 0)
        r = self.client.get(f"/api/sessions/{sid}/vector-config")
        self.assertEqual(r.get_json()["recent_n"], 0)

    def test_vector_config_clear_top_k_api(self):
        """前端清空 top_k 输入框传 null，应清除配置而非保留旧值。"""
        sid = self._new_session()
        self.client.post(f"/api/sessions/{sid}/vector-config",
                         json={"enabled": True, "top_k": 12})
        r = self.client.post(f"/api/sessions/{sid}/vector-config", json={"top_k": None})
        self.assertEqual(r.status_code, 200)
        self.assertIsNone(r.get_json()["top_k"])
        r = self.client.get(f"/api/sessions/{sid}/vector-config")
        self.assertIsNone(r.get_json()["top_k"])

    def test_summary_api_mock_llm(self):
        sid = self._new_session()
        for i in range(4):
            self.client.post(f"/api/sessions/{sid}/msg",
                             json={"role": "user", "content": f"提问{i}"})
            self.client.post(f"/api/sessions/{sid}/msg",
                             json={"role": "assistant", "content": f"回答{i}"})
        with mock.patch("llm_client.chat", return_value="一段剧情摘要"):
            r = self.client.post(f"/api/sessions/{sid}/summary", json={"api_key": "fake-key"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["summary"], "一段剧情摘要")
        # 缺 api_key
        r = self.client.post(f"/api/sessions/{sid}/summary", json={})
        self.assertEqual(r.status_code, 400)

    def test_summary_api_respects_switch_not_n_zero(self):
        """N=0 不再停用总结：手动总结照常执行；只有剧情摘要开关关闭才拒绝。"""
        sid = self._new_session()
        for i in range(4):
            self.client.post(f"/api/sessions/{sid}/msg",
                             json={"role": "user", "content": f"提问{i}"})
            self.client.post(f"/api/sessions/{sid}/msg",
                             json={"role": "assistant", "content": f"回答{i}"})
        # N=0 全量模式：手动总结照常执行（mock LLM 返回 200）
        self.client.post(f"/api/sessions/{sid}/vector-config", json={"recent_n": 0})
        with mock.patch("llm_client.chat", return_value="一段剧情摘要"):
            r = self.client.post(f"/api/sessions/{sid}/summary", json={"api_key": "fake-key"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["summary"], "一段剧情摘要")
        # 关闭剧情摘要开关：手动总结拒绝
        self.client.post(f"/api/sessions/{sid}/memory-switches", json={"summary_enabled": False})
        r = self.client.post(f"/api/sessions/{sid}/summary", json={"api_key": "fake-key"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("关闭", r.get_json()["error"])

    def test_summary_api_full_flag(self):
        """重新总结：/summary 传 full=true 时从全部历史重新生成并覆盖旧摘要。"""
        sid = self._new_session()
        for i in range(4):
            self.client.post(f"/api/sessions/{sid}/msg",
                             json={"role": "user", "content": f"提问{i}"})
            self.client.post(f"/api/sessions/{sid}/msg",
                             json={"role": "assistant", "content": f"回答{i}"})
        # 先增量生成一份旧摘要
        with mock.patch("llm_client.chat", return_value="旧摘要"):
            r = self.client.post(f"/api/sessions/{sid}/summary", json={"api_key": "fake-key"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["summary"], "旧摘要")
        # full=true 重新生成（覆盖旧摘要）
        with mock.patch("llm_client.chat", return_value="全新摘要"):
            r = self.client.post(f"/api/sessions/{sid}/summary",
                                 json={"api_key": "fake-key", "full": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["summary"], "全新摘要")
        mem = storage.get_session_memory(self.username, sid)
        self.assertEqual(mem["summary"]["text"], "全新摘要")

    def test_refresh_api_mock_llm(self):
        sid = self._new_session()
        for i in range(6):
            self.client.post(f"/api/sessions/{sid}/msg",
                             json={"role": "user", "content": f"提问{i}"})
            self.client.post(f"/api/sessions/{sid}/msg",
                             json={"role": "assistant", "content": f"回答{i}"})

        def fake_chat(api_key, model, messages, **kwargs):
            sys_prompt = messages[0]["content"]
            if "关键事实" in sys_prompt:
                return "- 主角叫小明\n- 小明是骑士"
            if "剧情摘要" in sys_prompt:
                return "小明踏上了冒险之旅。"
            return "ok"

        with mock.patch("llm_client.chat", side_effect=fake_chat):
            r = self.client.post(f"/api/sessions/{sid}/refresh", json={"api_key": "fake-key"})
        self.assertEqual(r.status_code, 200)
        body = r.get_json()
        self.assertTrue(body["facts"])
        self.assertTrue(body["summary"])
        self.assertEqual(body["errors"], [])
        # 缺 api_key
        r = self.client.post(f"/api/sessions/{sid}/refresh", json={})
        self.assertEqual(r.status_code, 400)

    def test_chat_api_stream_mock(self):
        """流式 chat：返回内容正确 + 收尾触发记忆维护。"""
        sid = self._new_session()

        def fake_stream(*args, **kwargs):
            yield "<<ANSWER>>你好"
            yield "<<ANSWER>>世界"

        with mock.patch("llm_client.chat", side_effect=fake_stream):
            with mock.patch.object(app_module, "_spawn_memory_maintenance") as spawn:
                r = self.client.post("/api/chat", json={
                    "api_key": "fake-key",
                    "model": "deepseek-chat",
                    "messages": [{"role": "user", "content": "你好"}],
                    "session_id": sid,
                })
                self.assertEqual(r.status_code, 200)
                # 必须在 with 块内消费响应（流式生成器懒执行，finally 触发记忆维护）
                text = r.get_data(as_text=True)
                # 流式协议：内容片段带 <<ANSWER>> 前缀（前端用于区分推理/回答）
                self.assertEqual(text, "<<ANSWER>>你好<<ANSWER>>世界")
                spawn.assert_called_once()
        # 缺参数
        r = self.client.post("/api/chat", json={"model": "deepseek-chat", "messages": []})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
