#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""claim_audit（声明↔留痕比对，P1 v2）的机器断言。

覆盖五类判据：
  ① 采集侧留痕：成功调用进 call_log 且 key 脱敏、失败调用不留痕、
     快照按 keep_raw 内嵌/省略 raw_calls；
  ② C1 正样本：声明为实采的值（段时长 / 距离 / 票价）如实进路书 → 退出码 0；
  ③ C1 负样本：实采 52 分钟路书写 48、实采 9.5 公里路书写 8.5、
     实采过路费 ¥17 路书写 ¥15 → **一律退出码 2**（数字幻觉在这里现形）；
  ④ 声明面本身：`_实采字段` 缺省 = 段时长（v1 向后兼容，距离不判死）、
     拼错的字段名不许被静默当成已声明；
  ⑤ 留痕加载：trace 形与快照形都认，**给错 flag 也不许静默丢**。
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / 'engine'))

from travel_planner.amap import AmapClient          # noqa: E402
from travel_planner.models import Location, Route   # noqa: E402
from travel_planner.workflow import collect_amap_snapshot  # noqa: E402

AUDIT = SKILL / 'tools' / 'claim_audit.py'


def _ok_body(params):
    """假高德返回体：status=1，带一个可辨识的字段。"""
    return {"status": "1", "count": "1",
            "geocodes": [{"location": "113.66,22.82"}]}


class _StubClient:
    """duck-typed client：只带 collect_amap_snapshot 用到的面。"""

    def __init__(self, with_calls=True):
        self.call_log = ([{
            "endpoint": "/v3/geocode/geo",
            "params": {"address": "东莞"},
            "fetched_at": "2026-09-30T00:00:00+00:00",
            "response": {"status": "1"},
        }] if with_calls else [])

    def resolve_location(self, text, city=None, expect_settlement=False):
        return Location(name=text, longitude=113.66, latitude=22.82, city=city)

    def route(self, origin, destination, mode="transit", city=None):
        return Route(mode=mode, origin=origin, destination=destination,
                     duration_minutes=52, distance_meters=40500)

    def search_around(self, center, keywords=None, types=None,
                      radius_meters=10000, limit=15):
        return []


class TestCallLog(unittest.TestCase):
    def test_success_calls_logged_and_key_redacted(self):
        seen = {}

        def transport(path, params):
            seen.update(params)
            return _ok_body(params)

        client = AmapClient("SECRET-KEY", transport=transport, sleep=lambda s: None)
        # 直打 _get：留痕钩子在这一层。geocode() 还带地名防误匹配守卫，
        # 假返回体过不了那道（那道守卫是另一个测试的事）。
        client._get("/v3/geocode/geo", {"address": "东莞市"})
        self.assertEqual(len(client.call_log), 1)
        rec = client.call_log[0]
        self.assertEqual(rec["endpoint"], "/v3/geocode/geo")
        self.assertNotIn("key", rec["params"])          # key 绝不入留痕
        self.assertNotIn("SECRET-KEY", json.dumps(rec))  # 双保险：整体序列化也无
        self.assertEqual(rec["response"]["status"], "1")

    def test_failed_call_leaves_no_trace(self):
        def transport(path, params):
            return {"status": "0", "info": "DAILY_QUERY_OVER_LIMIT",
                    "infocode": "10003"}

        client = AmapClient("SECRET-KEY", transport=transport, sleep=lambda s: None)
        with self.assertRaises(Exception):
            client.geocode("东莞市")
        self.assertEqual(client.call_log, [])            # 失败不构成证据


class TestSnapshotRawCalls(unittest.TestCase):
    def test_snapshot_embeds_raw_calls_by_default(self):
        snap = collect_amap_snapshot(
            {"origin": "广州市", "destination": "东莞市",
             "origin_city": "广州", "destination_city": "东莞"},
            _StubClient())
        self.assertTrue(snap["provenance"]["raw_retained"])
        self.assertEqual(snap["provenance"]["raw_call_count"], 1)
        self.assertEqual(len(snap["raw_calls"]), 1)

    def test_snapshot_optout_omits_key_entirely(self):
        snap = collect_amap_snapshot(
            {"origin": "广州市", "destination": "东莞市",
             "origin_city": "广州", "destination_city": "东莞"},
            _StubClient(), keep_raw=False)
        self.assertNotIn("raw_calls", snap)              # 「没留」可区分于「留了为空」
        self.assertFalse(snap["provenance"]["raw_retained"])


class TestClaimAuditGate(unittest.TestCase):
    """C1 正负样本走子进程实跑，退出码是闸门契约的一部分。"""

    def _run(self, facts, itinerary, nearby=None, snapshot=None):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            fp, ip = td / 'facts.json', td / 'itinerary.json'
            fp.write_text(json.dumps(facts, ensure_ascii=False), encoding='utf-8')
            ip.write_text(json.dumps(itinerary, ensure_ascii=False), encoding='utf-8')
            args = [sys.executable, str(AUDIT), '--facts', str(fp),
                    '--itinerary', str(ip)]
            if nearby:
                np = td / 'nearby.json'
                np.write_text(json.dumps(nearby, ensure_ascii=False), encoding='utf-8')
                args += ['--nearby', str(np)]
            proc = subprocess.run(args, capture_output=True, timeout=60)
            return proc.returncode, proc.stdout.decode('utf-8')

    @staticmethod
    def _itinerary(minutes):
        return {"_说明": "duration_minutes 全部来自高德驾车路线实采，不是估算。",
                "segments": [{"from_id": "a", "to_id": "b",
                              "duration_minutes": minutes}]}

    def test_faithful_claim_passes(self):
        facts = {"days": [{"slots": [
            {"time": "08:00",
             "body": {"full": "打车约 52 分钟（高德驾车路线，2026-09-30 查）[A]"}}]}]}
        code, out = self._run(facts, self._itinerary(52))
        self.assertEqual(code, 0, out)
        self.assertIn('PASS 1', out)

    def test_tampered_duration_fails_hard(self):
        facts = {"days": [{"slots": [
            {"time": "08:00",
             "body": {"full": "打车约 48 分钟（高德驾车路线，2026-09-30 查）[A]"}}]}]}
        code, out = self._run(facts, self._itinerary(52))
        self.assertEqual(code, 2, '采集 52 写成 48 必须FAIL——闸门不是摆设')
        self.assertIn('52 分钟', out)

    def test_rounded_same_value_passes(self):
        # 舍入规范内（52 → 「约 52 分钟」）不许误伤
        facts = {"days": [{"slots": [
            {"time": "08:00", "body": {"full": "车程 52分钟 [A]（高德）"}}]}]}
        code, _ = self._run(facts, self._itinerary(52))
        self.assertEqual(code, 0)

    def test_unverified_reported_without_failing(self):
        # 驾车距离不在**缺省**声明面（缺省 = duration_minutes，v1 口径）：
        # 列 UNVERIFIED，不许把它判成 FAIL——声明面没声明就不判死
        facts = {"days": [{"slots": [
            {"time": "08:00",
             "body": {"full": "打车约 52 分钟 / 9.5 公里（高德，2026-09-30 查）[A]"}}]}]}
        code, out = self._run(facts, self._itinerary(52))
        self.assertEqual(code, 0, out)
        self.assertIn('9.5公里', out)

    def test_nearby_names_report_only(self):
        # 顺道候选「只列不判断」：名字没进路书是正常事，不许 FAIL
        facts = {"days": [{"slots": [
            {"time": "08:00",
             "body": {"full": "打车约 52 分钟（高德）[A]"}}]}]}
        nearby = {"provider": "amap", "stops": [
            {"stop": "外滩", "places": [{"name": "没被选中的候选点",
                                         "distance_meters": 350}]}]}
        code, out = self._run(facts, self._itinerary(52), nearby=nearby)
        self.assertEqual(code, 0, out)


#: 一段真实形状的方向接口留痕：距离是**米**、时长是**秒**、过路费在 tolls。
#: 事实源写的却是「9.5 公里 / 16 分钟 / 过路费 ¥17」——两种写法之间的桥
#: 正是 v2 要修的东西，所以测试一律用这个跨单位的形状。
def _trace_call(distance="9500", duration="960", tolls="17",
                endpoint="/v3/direction/driving"):
    return {
        "endpoint": endpoint,
        "params": {"origin": "113.66,22.82", "destination": "113.75,22.90"},
        "fetched_at": "2026-10-01T02:00:00+00:00",
        "response": {"status": "1", "route": {"paths": [
            {"distance": distance, "duration": duration, "tolls": tolls}]}},
    }


def _trace(*calls):
    calls = calls or (_trace_call(),)
    return {"provider": "amap", "generated_at": "2026-10-01T10:00:00+08:00",
            "call_count": len(calls), "calls": list(calls)}


def _itinerary_v2(minutes=16, distance=9500, fare=17,
                  fields=("duration_minutes", "distance_meters", "fare_cny")):
    seg = {"from_id": "a", "to_id": "b"}
    if minutes is not None:
        seg["duration_minutes"] = minutes
    if distance is not None:
        seg["distance_meters"] = distance
    if fare is not None:
        seg["fare_cny"] = fare
    out = {"_说明": "各字段均来自高德方向接口实采，不是估算。",
           "segments": [seg]}
    if fields is not None:
        out["_实采字段"] = list(fields)
    return out


class TestClaimAuditV2(unittest.TestCase):
    """v2：留痕面扩到驾车距离与票价——声明驱动，负向样本必红。"""

    def _run(self, facts, itinerary, trace=None, use_snapshot_flag=False):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            fp, ip = td / 'facts.json', td / 'itinerary.json'
            fp.write_text(json.dumps(facts, ensure_ascii=False), encoding='utf-8')
            ip.write_text(json.dumps(itinerary, ensure_ascii=False), encoding='utf-8')
            args = [sys.executable, str(AUDIT), '--facts', str(fp),
                    '--itinerary', str(ip)]
            if trace is not None:
                tp = td / 'trace.json'
                tp.write_text(json.dumps(trace, ensure_ascii=False), encoding='utf-8')
                args += ['--snapshot' if use_snapshot_flag else '--trace', str(tp)]
            proc = subprocess.run(args, capture_output=True, timeout=60)
            return proc.returncode, proc.stdout.decode('utf-8')

    @staticmethod
    def _facts(text):
        return {"days": [{"slots": [
            {"time": "08:00", "body": {"full": text}}]}]}

    GOOD = "打车约 16 分钟 / 9.5 公里，过路费 ¥17（高德驾车路线，2026-10-01 查）[A]"

    def test_declared_distance_and_fare_pass(self):
        code, out = self._run(self._facts(self.GOOD), _itinerary_v2(), _trace())
        self.assertEqual(code, 0, out)
        self.assertIn('PASS 3', out)          # 时长 + 距离 + 票价三段值全可寻

    def test_tampered_distance_fails_hard(self):
        # 实采 9.5 公里、路书写 8.5 —— 与「采集 16 写成 15」同一个病
        facts = self._facts(
            "打车约 16 分钟 / 8.5 公里，过路费 ¥17（高德驾车路线，2026-10-01 查）[A]")
        code, out = self._run(facts, _itinerary_v2(), _trace())
        self.assertEqual(code, 2, '采集 9.5 公里写成 8.5 必须 FAIL——闸门不是摆设')
        self.assertIn('段距离', out)

    def test_tampered_fare_fails_hard(self):
        # 实采过路费 ¥17、路书写 ¥15
        facts = self._facts(
            "打车约 16 分钟 / 9.5 公里，过路费 ¥15（高德驾车路线，2026-10-01 查）[A]")
        code, out = self._run(facts, _itinerary_v2(), _trace())
        self.assertEqual(code, 2, '采集 ¥17 写成 ¥15 必须 FAIL')
        self.assertIn('段票价', out)

    def test_unused_leg_in_trace_is_not_a_failure(self):
        # 查过一段路不等于它必须进路书：未声明的段不许误伤
        extra = _trace_call(distance="40500", duration="2820", tolls="0")
        code, out = self._run(self._facts(self.GOOD), _itinerary_v2(),
                              _trace(_trace_call(), extra))
        self.assertEqual(code, 0, out)
        self.assertIn('PASS 3', out)

    def test_default_declaration_keeps_v1_behaviour(self):
        # 无 `_实采字段` → 缺省只认段时长：距离列 UNVERIFIED，**不判死**
        # （留痕里没有 12.3 公里这个值，但它没进声明面，所以只报告不 FAIL）
        itinerary = _itinerary_v2(fields=None)
        facts = self._facts("打车约 16 分钟 / 12.3 公里（高德驾车路线，2026-10-01 查）[A]")
        code, out = self._run(facts, itinerary, _trace())
        self.assertEqual(code, 0, out)
        self.assertIn('PASS 1', out)
        self.assertIn('12.3公里', out)

    def test_unknown_field_name_is_not_silently_declared(self):
        # 拼错字段名不许被静默当成已声明（否则声明面形同虚设）
        itinerary = _itinerary_v2(fields=("duration_minutes", "distance_meter"))
        code, out = self._run(self._facts(self.GOOD), itinerary, _trace())
        self.assertEqual(code, 0, out)
        self.assertIn('W7', out)
        self.assertIn('distance_meter', out)

    def test_trace_shape_accepted_via_snapshot_flag(self):
        # 2026-10-01 修：v1 只认快照形，trace 文件当 --snapshot 传会命中 W3
        # 被**静默忽略**——一份真留痕被丢掉却不报错，最不该是这个形状
        code, out = self._run(self._facts(self.GOOD), _itinerary_v2(), _trace(),
                              use_snapshot_flag=True)
        self.assertEqual(code, 0, out)
        self.assertIn('PASS 3', out)
        self.assertNotIn('W3', out)

    def test_fare_matches_trace_written_with_yuan_sign(self):
        # ¥17 是事实源的实际写法，v1 的正则只认「17 元」——票价整类看不见。
        # 有了留痕，时长/距离/票价三类必须**全部**对上，UNVERIFIED 归零：
        # 这正是 v2 的收益所在（v1 时代东莞 103 处、上海 18 处恒挂着）
        code, out = self._run(self._facts(self.GOOD), _itinerary_v2(), _trace())
        self.assertEqual(code, 0, out)
        c2_lines = [l for l in out.splitlines() if 'C2 声明回查' in l]
        self.assertIn('0 个', c2_lines[0], out)

    def test_fare_without_trace_stays_unverified(self):
        # 没进声明面、也没有留痕可对的票价：列 UNVERIFIED，不判死
        facts = self._facts("地铁 12 分钟 / 约 ¥3（高德路线，2026-10-01 查）[A]")
        itinerary = _itinerary_v2(minutes=12, distance=None, fare=None,
                                  fields=("duration_minutes",))
        code, out = self._run(facts, itinerary, None)
        self.assertEqual(code, 0, out)
        self.assertIn('¥3', out)

    def test_empty_trace_is_reported_not_silent(self):
        # 空留痕会让池子变空、看起来像「全都查不到」——必须说出来
        code, out = self._run(self._facts(self.GOOD), _itinerary_v2(),
                              {"provider": "amap", "generated_at": "x",
                               "call_count": 0, "calls": []})
        self.assertEqual(code, 0, out)
        self.assertIn('W5', out)

    def test_multiple_evidence_files_are_all_loaded(self):
        # ship.py 按 glob 逐个传留痕文件。参数若是单值，只有最后一个生效——
        # 多份留痕会被**静默丢掉**，那是本工具最不该有的形状。
        # 两份各带一半证据：都加载了 C2 才归零，丢一份就会挂出 9.5公里。
        first = _trace(_trace_call(tolls="0"))
        second = _trace(_trace_call(distance="1", duration="60", tolls="17"))
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            fp, ip = td / 'facts.json', td / 'itinerary.json'
            fp.write_text(json.dumps(self._facts(self.GOOD), ensure_ascii=False),
                          encoding='utf-8')
            ip.write_text(json.dumps(_itinerary_v2(), ensure_ascii=False),
                          encoding='utf-8')
            args = [sys.executable, str(AUDIT), '--facts', str(fp),
                    '--itinerary', str(ip)]
            for i, blob in enumerate((first, second)):
                tp = td / ('s%d.json' % i)
                tp.write_text(json.dumps(blob, ensure_ascii=False), encoding='utf-8')
                args += ['--snapshot', str(tp)]
            proc = subprocess.run(args, capture_output=True, timeout=60)
            out = proc.stdout.decode('utf-8')
        self.assertEqual(proc.returncode, 0, out)
        c2_lines = [l for l in out.splitlines() if 'C2 声明回查' in l]
        self.assertIn('0 个', c2_lines[0], out)


if __name__ == '__main__':
    unittest.main()
