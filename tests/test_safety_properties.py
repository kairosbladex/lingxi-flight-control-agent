"""安全短语表的属性测试。

`safety.py` 是绕过模型的承重件：命中就走确定性短路直接操作无人机。它的正确性原先
只靠十来条手写用例护着，而短语表是**鼓励按业务扩充**的，一旦有人加了会误命中的说法，
后果是危险动作被错误触发。这里把不变量写成可执行的属性，对**表中每一条短语**自动
生成变体来验证，新增短语会自动被覆盖。

不用 hypothesis：项目零依赖，而中文否定构式是可枚举的，穷举比随机生成覆盖得更实。
"""

from __future__ import annotations

import random
import unittest

from lingxi_flight_control import FlightControlAction, parse_flight_control_text
from lingxi_flight_control.full_control import ACTION_SPECS, _TEXT_ACTIONS
from lingxi_flight_control.safety import (
    DEFAULT_SAFETY_PHRASES,
    EXCLUDED_ACTIONS,
    SafetyFastPath,
    normalize,
)

LANE = SafetyFastPath()
PHRASES = [(phrase, action)
           for action, items in DEFAULT_SAFETY_PHRASES.items()
           for phrase in items]

#: 否定。这些构式一旦被当成肯定语义执行，就是误触发危险动作。
NEGATIONS = ("不", "别", "不要", "不用", "先别", "暂不", "暂时不", "请勿", "勿", "禁止", "无需", "没有")
#: 犹疑/延迟。用户还没下决心，不能替他执行。
HEDGES = ("先", "稍后", "等会", "等一下", "再", "暂时", "等我说了再", "打算", "准备", "我想")
#: 疑问。用户在问，不是在命令。
QUESTION_PREFIXES = ("能不能", "可否", "是否要", "要不要", "是否", "可以", "需要")
QUESTION_SUFFIXES = ("吗", "吧", "呢", "么")
#: 反问/转述：句子里出现了动作词，但语义不是命令。
QUOTED_PREFIXES = ("他说", "如果", "要是", "为什么", "什么时候", "怎么")


def negation_variants(phrase: str):
    for prefix in NEGATIONS:
        yield prefix + phrase
        yield prefix + "要" + phrase
    yield phrase + "不了"
    yield phrase + "不"
    yield "不" + phrase + "了"


def hedge_variants(phrase: str):
    for prefix in HEDGES:
        yield prefix + phrase
    yield phrase + "之前先等等"


def question_variants(phrase: str):
    for prefix in QUESTION_PREFIXES:
        yield prefix + phrase
    for suffix in QUESTION_SUFFIXES:
        yield phrase + suffix
    yield "问一下" + phrase


def quoted_variants(phrase: str):
    for prefix in QUOTED_PREFIXES:
        yield prefix + phrase


def embedded_variants(phrase: str):
    """短语被嵌进更长的句子里——精确匹配下必须不命中。"""
    yield "帮我" + phrase
    yield "请" + phrase
    yield "麻烦" + phrase
    yield phrase + "一下吧"
    yield phrase + "，然后继续航线"
    yield "现在" + phrase + "可以吗"
    yield "先" + phrase + "再说"


def all_hostile_variants(phrase: str):
    yield from negation_variants(phrase)
    yield from hedge_variants(phrase)
    yield from question_variants(phrase)
    yield from quoted_variants(phrase)


class PhraseCoverageTests(unittest.TestCase):
    """表里每一条短语都必须被识别，标点和空白不敏感。"""

    def test_every_phrase_is_recognized_with_its_action(self):
        for phrase, action in PHRASES:
            call = LANE.recognize(phrase)
            self.assertIsNotNone(call, phrase)
            self.assertEqual(call.name, action, phrase)
            self.assertEqual(dict(call.arguments), {}, phrase)

    def test_punctuation_whitespace_and_padding_are_irrelevant(self):
        for phrase, action in PHRASES:
            for decorated in (phrase + "！", phrase + "。", "  " + phrase + "  ",
                              phrase + "~", phrase + "?", "，" + phrase + "！"):
                call = LANE.recognize(decorated)
                self.assertIsNotNone(call, decorated)
                self.assertEqual(call.name, action, decorated)

    def test_the_table_is_not_empty(self):
        self.assertGreater(len(PHRASES), 0)
        self.assertEqual(set(DEFAULT_SAFETY_PHRASES), {action for _, action in PHRASES})


class NegationSafetyTests(unittest.TestCase):
    """核心安全属性：否定、犹疑、疑问、转述的变体绝不能被识别。"""

    def test_negation_hedge_question_and_quoted_variants_are_never_recognized(self):
        checked = 0
        for phrase, action in PHRASES:
            for variant in all_hostile_variants(phrase):
                checked += 1
                self.assertIsNone(LANE.recognize(variant),
                                  f"{variant!r} 由 {phrase!r}({action}) 派生，绝不该命中")
        self.assertGreater(checked, len(PHRASES) * 20, "变体覆盖太少，起不到作用")

    def test_embedded_phrases_are_not_recognized(self):
        for phrase, _action in PHRASES:
            for variant in embedded_variants(phrase):
                self.assertIsNone(LANE.recognize(variant), variant)

    def test_any_extra_material_breaks_the_match(self):
        """精确匹配是这套设计的安全根基：多一个有意义的字就不算命令。

        注意标点空白不算「额外材料」——归一化会剔除它们，那是刻意设计的容忍。
        """
        for phrase, _action in PHRASES:
            for suffix in ("a", "1", "。好", "等等", "呀", "的"):
                self.assertIsNone(LANE.recognize(phrase + suffix), phrase + suffix)

    def test_prefixing_a_single_character_breaks_the_match(self):
        for phrase, _action in PHRASES:
            self.assertIsNone(LANE.recognize("的" + phrase), phrase)
            self.assertIsNone(LANE.recognize("就" + phrase), phrase)


class TableInvariantTests(unittest.TestCase):
    """表本身的结构不变量，防止将来扩表时悄悄破坏前提。"""

    def test_no_phrase_maps_to_two_actions(self):
        with self.assertRaises(ValueError):
            SafetyFastPath({"a": ("急停",), "b": ("急停",)})
        with self.assertRaises(ValueError):
            SafetyFastPath({"a": ("急停",), "b": ("急 停",)})   # 归一化后同key

    def test_empty_tables_and_empty_phrases_are_rejected(self):
        for bad in ({"a": ()}, {"a": ("",)}, {"a": ("   ",)}, {"": ("急停",)}):
            with self.assertRaises(ValueError, msg=bad):
                SafetyFastPath(bad)

    def test_no_phrase_is_empty_after_normalization(self):
        for phrase, action in PHRASES:
            self.assertTrue(normalize(phrase), f"{action}: {phrase!r}")

    def test_every_action_name_is_a_real_flight_control_action(self):
        valid = {action.value for action in FlightControlAction}
        for action in DEFAULT_SAFETY_PHRASES:
            self.assertIn(action, valid)

    def test_every_safety_action_exists_in_the_cockpit_tool_surface(self):
        for action in DEFAULT_SAFETY_PHRASES:
            self.assertIn(FlightControlAction(action), ACTION_SPECS)

    def test_safety_phrases_do_not_shadow_other_actions(self):
        """安全短语不能把别的动作的指令抢走——那是错误触发，不是漏触发。"""
        for marker, action, _params in _TEXT_ACTIONS:
            recognized = LANE.recognize(marker)
            if recognized is not None:
                self.assertEqual(recognized.name, action.value,
                                 f"标记 {marker!r} 本应属于 {action.value}")

    def test_table_is_a_single_source_of_truth_with_the_text_parser(self):
        """parse_flight_control_text 必须与安全快路径认出同一批安全动作。"""
        for phrase, action in PHRASES:
            request = parse_flight_control_text(phrase, command_id="c", idempotency_key="k")
            self.assertEqual(request.action, FlightControlAction(action), phrase)
            # 必须是**合法**请求，不能是延迟到 to_dict/validate 才炸的坏对象。
            request.validate()

    def test_takeoff_is_deliberately_excluded_from_the_fast_path(self):
        """起飞需要高度参数，快路径只能给空参数；替用户猜一个起飞高度是危险的。"""
        self.assertNotIn("takeoff", DEFAULT_SAFETY_PHRASES)
        self.assertIn("takeoff", EXCLUDED_ACTIONS)
        self.assertIsNone(LANE.recognize("起飞"))
        self.assertIsNone(LANE.recognize("一键起飞"))
        # 常规解析仍要给出带默认高度的合法请求
        request = parse_flight_control_text("起飞", command_id="c", idempotency_key="k")
        request.validate()
        self.assertEqual(request.action, FlightControlAction.TAKEOFF)
        self.assertEqual(request.parameters["altitude_m"], 10)

    def test_every_fast_path_action_needs_no_parameter(self):
        """快路径只能发空参数，所以它认领的动作必须真的不需要参数。"""
        for action in DEFAULT_SAFETY_PHRASES:
            spec = ACTION_SPECS[FlightControlAction(action)]
            self.assertEqual(spec.required, (), f"{action} 需要参数，不能走快路径")

    def test_text_parser_refuses_every_negated_safety_phrase(self):
        for phrase, _action in PHRASES:
            for variant in negation_variants(phrase):
                with self.assertRaises(ValueError, msg=variant):
                    parse_flight_control_text(variant, command_id="c", idempotency_key="k")

    def test_recognized_actions_have_no_parameters(self):
        for phrase, _action in PHRASES:
            call = LANE.recognize(phrase)
            self.assertEqual(dict(call.arguments), {})


class NormalizationPropertyTests(unittest.TestCase):
    def test_normalization_is_idempotent(self):
        corpus = [p for p, _ in PHRASES] + [" 急 停！ ", "，返航。", "", "   ", "停止所有动作~"]
        for text in corpus:
            once = normalize(text)
            self.assertEqual(normalize(once), once, repr(text))

    def test_normalization_is_case_insensitive(self):
        self.assertEqual(normalize("ABC"), normalize("abc"))

    def test_recognize_is_deterministic(self):
        for phrase, _action in PHRASES:
            self.assertEqual(LANE.recognize(phrase), LANE.recognize(phrase))

    def test_none_and_non_string_inputs_never_match(self):
        for value in (None, "", "   ", "。", 0, 1.5, [], {}, object()):
            self.assertIsNone(LANE.recognize(value), repr(value))


class FuzzTests(unittest.TestCase):
    """随机文本的不变量：识别成功当且仅当归一化后正好是表里的某条短语。"""

    def test_random_text_recognition_matches_the_definition(self):
        table = {normalize(phrase): action for phrase, action in PHRASES}
        pool = sorted({character for phrase, _ in PHRASES for character in phrase}
                      | set("".join(NEGATIONS + HEDGES + QUESTION_PREFIXES + QUESTION_SUFFIXES))
                      | set("，。！？ 的了和就吗吧呢先别不"))
        rng = random.Random(20261014)
        for _ in range(20_000):
            text = "".join(rng.choice(pool) for _ in range(rng.randrange(0, 10)))
            expected = table.get(normalize(text))
            call = LANE.recognize(text)
            if expected is None:
                self.assertIsNone(call, f"{text!r} 不该命中")
            else:
                self.assertEqual(call.name, expected, text)

    def test_mutating_a_phrase_breaks_or_preserves_recognition_consistently(self):
        """逐字插入/删除都不得产生「命中但动作不对」的情况。"""
        rng = random.Random(7)
        intruders = "不别否无取消先再吗吧呢的了和就"
        for phrase, action in PHRASES:
            for _ in range(20):
                position = rng.randrange(len(phrase) + 1)
                mutated = phrase[:position] + rng.choice(intruders) + phrase[position:]
                call = LANE.recognize(mutated)
                if call is not None:
                    self.assertEqual(call.name, action, f"{mutated!r} 由 {phrase!r} 变异")


if __name__ == "__main__":
    unittest.main()
