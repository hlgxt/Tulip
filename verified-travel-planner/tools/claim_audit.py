#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
claim_audit.py — 声明↔留痕比对：机械复核「标了 [A] 的数是否真来自采集」。

为什么需要它
------------
source_audit.py 审的是**标注纪律**（徽章在册、时戳在场、等级够得着），
它管不了「标了 [A] 的那句是否真在所引来源里」——README 待办 P1，本 skill
已知的最大缺口。成因：事实源只存结论、不存原始返回，没有可比对的东西。

本工具是补这个缺口的一层，配两半：
  采集侧：每次成功调用都进 client.call_log（amap.py）；amap-snapshot 内嵌
    raw_calls；search-places / nearby-spots / route 可 --trace 落盘。
  比对侧（本文件）：把留痕里的实采值拿到事实源全文里找。

查什么（v2 范围，宁缺毋滥——每条 FAIL 都必须钉得住）
--------------------------
  C1 实采值覆盖   itinerary.segments 里**被声明为实采**的字段值（见下
                  `_实采字段`）必须逐个出现在事实源全文中（按数值规范比对）。
                  找不到 = 采集到的数没如实进路书——采集 16 写成 15 这类
                  数字幻觉，在这里现形。FAIL。
  C2 声明回查     事实源里含「高德/amap」字样的字符串中的数值（分钟/公里/
                  米/元，以及 ¥/￥ 前缀的票价），逐个回查留痕池；查不到的
                  列 UNVERIFIED——没有留痕的声明只能停在「说得出来源」，
                  如实列出，不判死。
  C3 留痕健康     itinerary 无「实采」声明、快照无 raw_calls、周边采集缺
                  provider 字段、留痕文件空转 → WARN（留痕缺位，声明只能
                  停在「说得出来源」）。

`_实采字段`（v2 新增，机器可读的声明面）
--------------------------------------
  itinerary 顶层用 `"_实采字段": ["duration_minutes", "distance_meters",
  "fare_cny"]` 声明**哪些字段是实采的**。**缺省 = `["duration_minutes"]`**，
  与 v1 行为逐字一致（老文件零影响）。
  为什么要有它：查过一段路不等于那段必须进路书（查了不用是正常事），
  所以只有**显式声明为实采**的字段才受 C1 约束——否则闸门会开始误伤。
  认得的字段：`duration_minutes` / `distance_meters` / `fare_cny`
  （fare_cny 对驾车是过路费 tolls、对公交是票价 cost）。

不查（边界，写在这里是为了不被读成保证）
--------------------------------------
· 非高德来源的声明（**官网门票价等**）——归 source_audit 的 E 规则。
  门票价结构性落在高德能力之外，本工具**给不了它留痕**：要补得先有
  「网页正文摘录」通道，那是独立的一层，别把过 C1 读成门票价也核过了。
· 快照/留痕与世界是否一致——留痕只证明「声明与采集一致」，不证明「采集对」
· nearby 的候选点名/评分/距离、快照的城级路线值——采集了但没进路书是正常
  事（顺道候选只列不判断），只报告不计 FAIL
· 「值在文中出现过」是存在性判据：同一个数在别处（日期、电话）出现过也算寻到。
  它拦的是**改写与丢失**，不是「这个数对不对」——后者要回查原始返回体

用法
----
    python tools/claim_audit.py --facts 路书_X.json --itinerary itinerary_X.json
                                [--nearby nearby_X.json ...]
                                [--trace 留痕_X.json ...] [--snapshot 快照_X.json ...]
                                [--json]
    `--trace` 与 `--snapshot` 都按**形状**自动识别（trace 形 {calls[]} /
    快照形 {provenance, routes[], raw_calls[]}），给哪个都行、给错也不丢。
退出码：0 = 无 FAIL ｜ 2 = 有 FAIL ｜ 1 = 用法/读取错误
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

NUM_TOKEN = re.compile(r'\d+(?:\.\d+)?')
UNIT_NUM = re.compile(r'(\d+(?:\.\d+)?)\s*(分钟|小时|公里|千米|米|元)')
#: 票价在事实源里写作 `过路费 ¥17` / `约 ¥3`——**不是**「17 元」。
#: v1 只认 `数字+元`，于是东莞 103 处 UNVERIFIED 里 `元` 是 0 条：票价整类
#: 从未进入视野。这条正则把它补上（2026-10-01，P1 v2）。
FARE_NUM = re.compile(r'[¥￥]\s*(\d+(?:\.\d+)?)')
AMAP_WORDS = ('高德', 'amap')
REALDATA_MARKS = ('实采', '高德')


def _fmt(f: float) -> str:
    s = ('%f' % float(f)).rstrip('0').rstrip('.')
    return s if s else '0'


def _duration_forms(value) -> set:
    return {_fmt(value), str(value)}


def _distance_forms(value) -> set:
    """米 → 事实源可能写的各种形：原米数、公里一位/两位小数。"""
    meters = float(value)
    return {
        _fmt(meters), str(value),
        _fmt(meters / 1000.0),
        _fmt(round(meters / 1000.0, 1)),
        _fmt(round(meters / 1000.0, 2)),
    }


def _fare_forms(value) -> set:
    return {_fmt(value), str(value)}


#: itinerary `_实采字段` 认得的字段 → (中文标签, 值 → 可接受的文本形, 单位)。
#: 候选形要覆盖事实源里的**实际写法**：留痕给的是 9500 米，路书写的是
#: 「9.5 公里」；票价写「¥17」。少一种写法就是一次误伤。
#: 缺省只认 duration_minutes —— 与 v1 逐字一致，老文件零影响。
_DECLARABLE_FIELDS = {
    'duration_minutes': ('段时长', _duration_forms, '分钟'),
    'distance_meters': ('段距离', _distance_forms, '米'),
    'fare_cny': ('段票价', _fare_forms, '元'),
}
_DEFAULT_DECLARED = ('duration_minutes',)


def claim_values(text: str):
    """句子里值得回查留痕的数值声明 → [(值文本, 展示标签, 起, 止)]。

    两类：带中文单位的（分钟/小时/公里/千米/米/元），以及 ¥/￥ 前缀的票价。
    """
    for m in UNIT_NUM.finditer(text):
        yield m.group(1), '%s%s' % (m.group(1), m.group(2)), m.start(), m.end()
    for m in FARE_NUM.finditer(text):
        yield m.group(1), '¥%s' % m.group(1), m.start(), m.end()


def _excerpt(text: str, start: int, end: int, width: int = 52) -> str:
    """以**命中处为中心**截一段，别只给字符串开头。

    事实源里的 section 正文是整块 HTML，开头 60 字常常是 `<div class=...><h1>`
    这类标签——人对着那段找不着被报的数，会以为工具报错了。
    """
    pad = max(0, width - (end - start))
    left = max(0, start - pad // 2)
    right = min(len(text), end + (pad - (start - left)))
    fragment = ' '.join(text[left:right].split())
    return ('…' if left else '') + fragment + ('…' if right < len(text) else '')


def iter_strings(obj):
    """深度遍历 JSON，产出全部字符串值（跳过 _说明/_使用说明 类自述键）。"""
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, list):
        for v in obj:
            yield from iter_strings(v)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.startswith('_'):
                continue
            yield from iter_strings(v)


def number_tokens(text: str) -> set:
    """文本里出现的全部数值（原样 + 常见舍入形），供「值在文中」判断。"""
    vals = set()
    for m in NUM_TOKEN.finditer(text):
        s = m.group(0)
        vals.add(s)
        if '.' in s:
            f = float(s)
            vals.add(_fmt(f))
            vals.add(_fmt(round(f, 1)))
            vals.add(_fmt(round(f)))
    return vals


class Evidence:
    """留痕池：从各证据文件里取出的「机器实采值」。"""

    def __init__(self):
        self.durations = set()          # 分钟（残留痕 / 周边采集）
        self.distances_m = set()        # 米（快照路线 / 周边采集）
        self.costs = set()              # 元（快照路线）
        self.names = []                 # POI 名（周边采集）
        self.raw_texts = []             # 原始返回体文本（快照 raw_calls / trace calls）
        self.claims = []                # [(标签, 值, 可接受文本形)] 声明为实采的段值
        self.declared_fields = []       # itinerary 声明了哪些字段（照抄，供报数）
        self.itinerary_declared = None  # itinerary 是否自声明「实采」
        self.snapshot_raw = None        # 快照是否内嵌 raw_calls
        self.warnings = []

    @property
    def empty(self) -> bool:
        return not (self.durations or self.distances_m or self.costs
                    or self.names or self.raw_texts)


def load_itinerary(path: Path, ev: Evidence) -> None:
    """读行程留痕：按 `_实采字段` 声明的字段收集「必须如实进路书」的值。

    `_实采字段` 缺省 = ["duration_minutes"]，与 v1 行为逐字一致。
    声明面存在的理由：**查过一段路不等于那段必须进路书**（查了不用是正常事），
    只有显式声明为实采的字段才受 C1 约束——否则闸门会开始误伤。
    """
    d = json.loads(path.read_text(encoding='utf-8'))
    segments = d.get('segments') or []
    note = str(d.get('_说明') or '')
    ev.itinerary_declared = any(m in note for m in REALDATA_MARKS)

    declared = d.get('_实采字段')
    if declared is None:
        declared = list(_DEFAULT_DECLARED)
    elif isinstance(declared, str):
        declared = [declared]
    elif not isinstance(declared, list):
        ev.warnings.append(
            'W6 %s 的 `_实采字段` 不是数组——按缺省 %s 处理，声明面未生效'
            % (path.name, '/'.join(_DEFAULT_DECLARED)))
        declared = list(_DEFAULT_DECLARED)

    unknown = [f for f in declared if f not in _DECLARABLE_FIELDS]
    if unknown:
        ev.warnings.append(
            'W7 %s 的 `_实采字段` 含未认得的字段 %s——认得的只有 %s，'
            '拼错的字段不会被静默当成已声明'
            % (path.name, '、'.join(str(u) for u in unknown),
               '、'.join(_DECLARABLE_FIELDS)))
    ev.declared_fields = [f for f in declared if f in _DECLARABLE_FIELDS]

    if not ev.itinerary_declared:
        ev.warnings.append(
            'W1 %s 没有「实采」自声明（_说明）——段值来源不明，'
            'C1 覆盖审计降为跳过' % path.name)
        return

    for seg in segments:
        if not isinstance(seg, dict):
            continue
        for field in ev.declared_fields:
            value = seg.get(field)
            if value is None:
                continue
            label_text, forms, unit = _DECLARABLE_FIELDS[field]
            try:
                forms_of_value = forms(value)
            except (TypeError, ValueError):
                ev.warnings.append(
                    'W8 %s 的段 %s→%s 的 %s 不是数（%r）——无法比对'
                    % (path.name, seg.get('from_id'), seg.get('to_id'),
                       field, value))
                continue
            ev.claims.append((
                '%s %s %s（%s→%s）' % (label_text, _fmt(value), unit,
                                      seg.get('from_id'), seg.get('to_id')),
                value, forms_of_value))
            if field == 'duration_minutes':
                ev.durations.add(value)

    if ev.declared_fields and not ev.claims:
        # 声明了字段却一个值都没有：留痕与声明对不上，要看得见
        ev.warnings.append(
            'W9 %s 声明了 `_实采字段`=%s，但没有一段带上这些字段——'
            'C1 无值可查（是漏写还是本该为空？）'
            % (path.name, '/'.join(ev.declared_fields)))


def load_nearby(path: Path, ev: Evidence) -> None:
    d = json.loads(path.read_text(encoding='utf-8'))
    if str(d.get('provider') or '') != 'amap':
        ev.warnings.append('W2 %s 缺 provider=amap 字段——来源身份不明' % path.name)
    for stop in d.get('stops') or []:
        for place in (stop.get('places') or []):
            name = str(place.get('name') or '').strip()
            if name:
                ev.names.append(name)
            dist = place.get('distance_meters')
            if isinstance(dist, (int, float)) and dist > 0:
                ev.distances_m.add(dist)


def load_snapshot(path: Path, ev: Evidence) -> None:
    d = json.loads(path.read_text(encoding='utf-8'))
    prov = d.get('provenance') or {}
    if str(prov.get('provider') or '') != 'amap':
        ev.warnings.append('W3 %s 非 amap 快照——不在本工具比对范围' % path.name)
        return
    raw_calls = d.get('raw_calls')
    ev.snapshot_raw = raw_calls is not None
    if not ev.snapshot_raw:
        ev.warnings.append(
            'W4 %s 未内嵌 raw_calls（生成时用了 --no-keep-raw 或旧版工具）——'
            '原始返回体缺位，只能比对蒸馏值' % path.name)
    else:
        for call in raw_calls:
            try:
                ev.raw_texts.append(json.dumps(call.get('response'), ensure_ascii=False))
            except (TypeError, ValueError):
                pass
    for route in d.get('routes') or []:
        if not isinstance(route, dict):
            continue
        if route.get('duration_minutes') is not None:
            ev.durations.add(route['duration_minutes'])
        if route.get('distance_meters') is not None:
            ev.distances_m.add(route['distance_meters'])
        if route.get('estimated_cost') is not None:
            ev.costs.add(route['estimated_cost'])


def load_trace(path: Path, ev: Evidence) -> None:
    """读 `--trace` 形留痕：{provider, generated_at, call_count, calls[]}。

    `calls[].response` 是**完整原始返回体**——方向接口的 distance / duration /
    tolls / cost 都在里面，所以直接进 raw_texts 就能被 C2 的池子吃到。
    """
    d = json.loads(path.read_text(encoding='utf-8'))
    if str(d.get('provider') or '') != 'amap':
        ev.warnings.append('W3 %s 非 amap 留痕——不在本工具比对范围' % path.name)
        return
    calls = d.get('calls')
    if not isinstance(calls, list):
        ev.warnings.append('W5 %s 没有 calls 数组——留痕文件形状不对' % path.name)
        return
    if not calls:
        # 空留痕要说出来：它会让 C2 池子变空，看起来像「全都查不到」
        ev.warnings.append(
            'W5 %s 里 0 条成功调用（call_count=%s）——本次采集没留下任何证据，'
            '不是「查了都对得上」' % (path.name, d.get('call_count')))
        return
    for call in calls:
        if not isinstance(call, dict):
            continue
        try:
            ev.raw_texts.append(json.dumps(call.get('response'), ensure_ascii=False))
        except (TypeError, ValueError):
            pass
        # 蒸馏值也收一份：C2 的池子会用到，且未来按接口分型比对时不必再解析
        endpoint = str(call.get('endpoint') or '')
        response = call.get('response')
        if '/direction/' in endpoint and isinstance(response, dict):
            _harvest_route_values(response, ev)


def _harvest_route_values(response: dict, ev: Evidence) -> None:
    """从方向接口返回体里取蒸馏值（距离/时长/过路费·票价）。

    为什么必须收：原始返回体里距离是**米**（"distance":"9500"）、时长是**秒**
    （"duration":"960"），而事实源写的是「9.5 公里 / 16 分钟」。只把原始体
    丢进池子，两个写法的桥就断了——距离/票价会永远停在 UNVERIFIED。
    秒→分用 `math.ceil`，与 `amap.py` 的 `_minutes` **逐字一致**：这里是
    复算引擎已经写进路书的那个数，取整方式差一点就会自己造出误报。
    """
    route = response.get('route') or {}
    for path in (route.get('paths') or []):
        if not isinstance(path, dict):
            continue
        _add_route_pair(ev, path.get('duration'), path.get('distance'),
                        path.get('tolls'))
    for transit in (route.get('transits') or []):
        if not isinstance(transit, dict):
            continue
        _add_route_pair(ev, transit.get('duration'), transit.get('distance'),
                        transit.get('cost'))


def _add_route_pair(ev: Evidence, duration, distance, cost) -> None:
    seconds = _to_number(duration)
    if seconds is not None:
        ev.durations.add(max(0, math.ceil(seconds / 60)))
    meters = _to_number(distance)
    if meters is not None:
        ev.distances_m.add(int(meters))
    fare = _to_number(cost)
    if fare is not None:
        ev.costs.add(fare)


def _to_number(value):
    if value in (None, '', []):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def load_evidence(path: Path, ev: Evidence) -> str:
    """**按形状**加载留痕文件，返回 'trace' / 'snapshot' / 'unknown'。

    两种形状各有出处，不能猜错：
      · trace 形：{provider, generated_at, call_count, calls[]}——`--trace` 落盘
      · 快照形：{provenance:{provider:amap}, routes[], raw_calls[]}——amap-snapshot

    2026-10-01 修：v1 只认快照形，把 trace 文件当 `--snapshot` 传会命中 W3 被
    **静默忽略**——一份真实留痕被丢掉却不报错，正是本工具最该避免的形状。
    """
    d = json.loads(path.read_text(encoding='utf-8'))
    if isinstance(d.get('calls'), list):
        load_trace(path, ev)
        return 'trace'
    if 'provenance' in d or 'raw_calls' in d or 'routes' in d:
        load_snapshot(path, ev)
        return 'snapshot'
    ev.warnings.append(
        'W3 %s 既不是 trace 形也不是快照形——无法识别，未加载' % path.name)
    return 'unknown'


def check_c1_coverage(facts_text: str, tokens: set, ev: Evidence):
    """C1：声明为实采的段值必须逐个出现在事实源全文。返回 (pass, fail)。

    覆盖面由 itinerary 的 `_实采字段` 决定（缺省 = 段时长，即 v1 口径）。
    """
    ok, bad = [], []
    for label, value, forms in ev.claims:
        if forms & tokens:
            ok.append('%s 在事实源中可寻' % label)
        else:
            bad.append('%s 在事实源全文中找不到——被改数、被丢段，'
                       '或该段已不进路书（修数据或删留痕）' % label)
    return ok, bad


def check_c2_claims(facts_strings, ev: Evidence):
    """C2：高德句中的数值（含 ¥/￥ 票价）回查留痕池。返回 UNVERIFIED（不判死）。"""
    pool_numbers = set()
    for v in ev.durations:
        pool_numbers.add(_fmt(v))
    for m in ev.distances_m:
        pool_numbers.add(_fmt(m))
        pool_numbers.add(_fmt(round(m / 1000.0, 1)))
        pool_numbers.add(_fmt(round(m / 1000.0, 2)))
    for c in ev.costs:
        pool_numbers.add(_fmt(c))
    for text in ev.raw_texts:
        pool_numbers.update(m.group(0) for m in NUM_TOKEN.finditer(text))

    unverified = []
    for s in facts_strings:
        if not any(w in s for w in AMAP_WORDS):
            continue
        for raw_value, label, start, end in claim_values(s):
            if _fmt(float(raw_value)) not in pool_numbers:
                unverified.append('%s ｜ %s' % (label, _excerpt(s, start, end)))
    return unverified


def main():
    ap = argparse.ArgumentParser(
        description='声明↔留痕比对：机械复核 [A] 声明是否真来自采集（P1 v2）')
    ap.add_argument('--facts', required=True, help='路书事实源 JSON')
    ap.add_argument('--itinerary',
                    help='行程留痕；用 `_实采字段` 声明哪些字段是实采的'
                         '（缺省 duration_minutes）')
    ap.add_argument('--nearby', action='append', default=[],
                    help='周边采集留痕（可多次）')
    ap.add_argument('--trace', action='append', default=[],
                    help='route / search-places / nearby-spots --trace 落下的留痕'
                         '（可多次；形状自动识别）')
    ap.add_argument('--snapshot', action='append', default=[],
                    help='amap-snapshot 快照（可多次；形状自动识别，'
                         '给 trace 文件也不会丢）')
    ap.add_argument('--json', action='store_true', help='输出机器可读 JSON')
    a = ap.parse_args()

    facts_path = Path(a.facts)
    if not facts_path.is_file():
        print('事实源不存在：%s' % facts_path)
        return 1
    ev = Evidence()
    loaded = 0
    # 留痕文件现在由 ship.py 按命名约定**自动发现**，所以读不动的那份必须
    # 指名道姓地报出来——裸 traceback 会让人对着七八个文件猜是哪个坏了。
    try:
        if a.itinerary:
            load_itinerary(Path(a.itinerary), ev)
            loaded += 1
        for n in a.nearby:
            load_nearby(Path(n), ev)
            loaded += 1
        for t in a.trace:
            load_evidence(Path(t), ev)
            loaded += 1
        for s in a.snapshot:
            load_evidence(Path(s), ev)
            loaded += 1
    except OSError as exc:
        print('留痕文件读不了：%s' % exc)
        return 1
    except json.JSONDecodeError as exc:
        print('留痕文件不是合法 JSON（%s）——修好它，或把不该被发现的文件移走' % exc)
        return 1
    if loaded == 0:
        print('没有任何留痕文件（--itinerary/--nearby/--trace/--snapshot 至少给一个）')
        return 1

    facts = json.loads(facts_path.read_text(encoding='utf-8'))
    facts_strings = list(iter_strings(facts))
    facts_text = '\n'.join(facts_strings)
    tokens = number_tokens(facts_text)

    c1_ok, c1_bad = check_c1_coverage(facts_text, tokens, ev)
    c2_unverified = check_c2_claims(facts_strings, ev)

    result = {
        'facts': str(facts_path),
        'evidence_files': loaded,
        'declared_fields': ev.declared_fields,
        'c1_coverage': {'pass': c1_ok, 'fail': c1_bad},
        'c2_unverified': c2_unverified,
        'warnings': ev.warnings,
        'ok': not c1_bad,
        'scope': ('v2：C1 钉「声明为实采的段值未如实进路书」（覆盖 duration_minutes / '
                  'distance_meters / fare_cny，由 itinerary 的 `_实采字段` 决定）；'
                  'C2 的 UNVERIFIED 是诚实缺口不是 FAIL；**门票价不在本工具能力内**'
                  '（官网/OTA 渠道，需网页摘录留痕才能覆盖）；留痕证明「声明与采集'
                  '一致」，不证明「采集与世界一致」。'),
    }

    if a.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result['ok'] else 2

    print('=' * 74)
    print('声明↔留痕比对（claim_audit v2）｜ 事实源：%s' % facts_path.name)
    print('=' * 74)
    print('  声明面：%s' % ('、'.join(ev.declared_fields) or '（未声明，C1 无值可查）'))
    print('  C1 实采值覆盖：PASS %d ｜ FAIL %d' % (len(c1_ok), len(c1_bad)))
    for row in c1_ok:
        print('    [OK] %s' % row)
    for row in c1_bad:
        print('    [X ] %s' % row)
    print('  C2 声明回查：%d 个高德句数值无留痕可对（UNVERIFIED，不判死）'
          % len(c2_unverified))
    for row in c2_unverified[:8]:
        print('    [? ] %s' % row)
    if len(c2_unverified) > 8:
        print('    …另有 %d 处' % (len(c2_unverified) - 8))
    if ev.warnings:
        print('  C3 留痕健康：')
        for w in ev.warnings:
            print('    [%s]' % w)
    print('-' * 74)
    if c1_bad:
        print('[X] FAIL %d 处——实采值没有如实进路书，先修再交付' % len(c1_bad))
        return 2
    print('[OK] 实采值全部可寻（%d 个）｜ 声明与留痕一致' % len(c1_ok))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
