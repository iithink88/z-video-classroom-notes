#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""z-video-classroom-notes 单元测试。

运行：python tests/test_lecture_notes.py
"""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True  # 避免沙箱拦截往技能目录写 __pycache__

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPT = SKILL_DIR / "scripts" / "lecture_notes.py"


def load_module():
    spec = importlib.util.spec_from_file_location("lecture_notes", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot-load-script")
    module = importlib.util.module_from_spec(spec)
    # 必须先注册进 sys.modules：脚本里有 @dataclass，dataclasses 需要反查模块命名空间
    sys.modules["lecture_notes"] = module
    spec.loader.exec_module(module)
    return module


ln = load_module()


class TestTimeUtils(unittest.TestCase):
    def test_label_roundtrip(self):
        for seconds in (0, 5, 65, 600, 3725):
            self.assertEqual(ln.label_to_seconds(ln.seconds_to_label(seconds)), seconds)

    def test_label_to_seconds_full(self):
        self.assertEqual(ln.label_to_seconds("01:02:03"), 3723.0)

    def test_label_to_seconds_bad(self):
        self.assertEqual(ln.label_to_seconds(""), 0.0)
        self.assertEqual(ln.label_to_seconds("abc"), 0.0)


class TestJsonExtraction(unittest.TestCase):
    def test_fenced(self):
        raw = '```json\n{"a": 1}\n```'
        self.assertEqual(ln.extract_json_block(raw), {"a": 1})

    def test_trailing_comma(self):
        raw = '{"a": 1, "b": [1,2,],}'
        self.assertEqual(ln.extract_json_block(raw)["a"], 1)

    def test_surrounded_by_text(self):
        raw = '好的，结果如下：\n{"section_title": "X", "terms": []}\n希望有帮助'
        self.assertEqual(ln.extract_json_block(raw)["section_title"], "X")

    def test_unparseable_raises(self):
        with self.assertRaises(json.JSONDecodeError):
            ln.extract_json_block("完全没有 JSON")


class TestPlan(unittest.TestCase):
    def test_short_media_single_segment(self):
        plan = ln.build_plan(20.0, 14, 60.0)
        self.assertEqual(len(plan), 1)
        self.assertEqual(plan[0], (0.0, 20.0))

    def test_long_media_capped(self):
        plan = ln.build_plan(3600.0, 14, 60.0)
        self.assertEqual(len(plan), 14)
        self.assertAlmostEqual(plan[-1][1], 3600.0)

    def test_plan_is_contiguous(self):
        plan = ln.build_plan(600.0, 8, 60.0)
        for (s1, e1), (s2, _e2) in zip(plan, plan[1:]):
            self.assertAlmostEqual(e1, s2)


class TestNormalizeSection(unittest.TestCase):
    def test_bad_frame_id_dropped(self):
        raw = {
            "section_title": "测试节",
            "anchors": [{"time": "99:99", "label": "越界", "frame_id": "frame-999"}],
        }
        section = ln.normalize_section(raw, 0, 10.0, 100.0, ["frame-001"])
        self.assertEqual(section["anchors"][0]["frame_id"], "")
        self.assertEqual(section["anchors"][0]["time"], "01:40")  # 被夹回区间内

    def test_missing_fields_default(self):
        section = ln.normalize_section({}, 2, 0.0, 60.0, [])
        self.assertEqual(section["segment_index"], 2)
        self.assertEqual(section["section_title"], "第 3 节")
        self.assertEqual(section["terms"], [])

    def test_string_fields_wrapped(self):
        section = ln.normalize_section({"principles": "只有一条"}, 0, 0.0, 60.0, [])
        self.assertEqual(section["principles"], ["只有一条"])


class TestMerge(unittest.TestCase):
    def test_exam_points_dedup_and_group(self):
        sections = [{"exam_points": [{"type": "考点", "text": "A"}, {"type": "乱写", "text": "B"}]}]
        merged = ln.merge_exam_points(sections, [{"type": "考点", "text": "A"}])
        self.assertEqual(len(merged), 2)  # A 去重，B 归为「重要」
        kinds = {m["type"] for m in merged}
        self.assertTrue({"考点", "重要"} <= kinds)

    def test_section_order_override(self):
        sections = [
            {"segment_index": 0, "section_title": "旧0"},
            {"segment_index": 1, "section_title": "旧1"},
        ]
        ordered = ln.apply_section_order(sections, [{"segment_index": 1, "title": "新1"}, {"segment_index": 0, "title": "新0"}])
        self.assertEqual([s["section_title"] for s in ordered], ["新1", "新0"])

    def test_section_order_fallback(self):
        sections = [{"segment_index": 0, "section_title": "旧0"}]
        self.assertEqual(ln.apply_section_order(sections, "坏数据"), sections)


class TestRender(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cn-notes-"))
        (self.tmp / "assets").mkdir(parents=True, exist_ok=True)
        self.media = self.tmp / "lesson.mp4"
        self.media.write_bytes(b"fake")

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def _render(self) -> str:
        plan = ln.build_plan(300.0, 14, 60.0)
        analysis = ln.mock_notes("测试课程", 300.0, "video", plan)
        return ln.render_html(
            analysis=analysis,
            title="测试课程",
            kind="video",
            media=self.media,
            out_dir=self.tmp,
            frames={},
            transcript={"segments": [{"start": 0, "end": 5, "text": "第一句"}], "text": "第一句", "_duration": 300.0},
            math_enabled=True,
            mermaid_enabled=True,
        )

    def test_all_blocks_present(self):
        html_text = self._render()
        for anchor in ("framework", "timeline", "notes", "exam", "quiz", "homework", "glossary", "caveats", "transcript"):
            self.assertIn(f'id="{anchor}"', html_text)

    def test_media_and_seek_present(self):
        html_text = self._render()
        self.assertIn("src-media", html_text)
        self.assertIn("data-seek", html_text)

    def test_no_secret_pattern(self):
        html_text = self._render()
        for pattern in ln.SECRET_PATTERNS:
            self.assertIsNone(pattern.search(html_text))

    def test_escapes_model_text(self):
        html_text = self._render()
        self.assertNotIn("<script>alert", html_text.replace("<script>" + ln.JS, ""))

    def test_rich_inline_format(self):
        self.assertEqual(ln.rich("**要点**"), "<strong>要点</strong>")
        self.assertEqual(ln.rich("用 `useCurrentFrame()` 取帧"), "用 <code>useCurrentFrame()</code> 取帧")

    def test_rich_does_not_open_xss(self):
        out = ln.rich("**<img src=x onerror=alert(1)>**")
        self.assertNotIn("<img", out)
        self.assertIn("&lt;img", out)

    def test_formula_code_block_renders(self):
        analysis = ln.mock_notes("测试课程", 300.0, "video", ln.build_plan(300.0, 14, 60.0))
        analysis["sections"][0]["formulas"] = [{"name": "片段", "code": "const f = useCurrentFrame();", "note": "取当前帧"}]
        html_text = ln.render_html(
            analysis=analysis,
            title="测试课程",
            kind="video",
            media=self.media,
            out_dir=self.tmp,
            frames={},
            transcript={"segments": [], "text": "", "_duration": 300.0},
            math_enabled=False,
            mermaid_enabled=False,
        )
        self.assertIn("codeblock", html_text)
        self.assertIn("useCurrentFrame", html_text)

    def test_validate_detects_missing_asset(self):
        html_path = self.tmp / "t.html"
        html_path.write_text("<img src='assets/nope.jpg'>", encoding="utf-8")
        issues = ln.validate_html(html_path, self.tmp)
        self.assertTrue(any("missing-local-asset" in i for i in issues))

    def test_validate_ok_for_existing(self):
        (self.tmp / "assets" / "a.jpg").write_bytes(b"x")
        html_path = self.tmp / "t.html"
        html_path.write_text("<img src='assets/a.jpg'><a href='#x'>y</a>", encoding="utf-8")
        self.assertEqual(ln.validate_html(html_path, self.tmp), [])

    def test_media_src_relative(self):
        src = ln.media_src(self.media, self.tmp)
        self.assertEqual(src, "lesson.mp4")

    def test_media_src_parent_dir(self):
        nested = self.tmp / "assets"
        src = ln.media_src(self.media, nested)
        self.assertEqual(src, "../lesson.mp4")


class TestPromptTemplates(unittest.TestCase):
    """回归用例：prompt 模板含 JSON 大括号，绝不能用 str.format。"""

    def test_section_messages_build(self):
        messages = ln.build_section_messages(
            title="测试课",
            subject="物理",
            audience="八年级",
            extra="",
            index=0,
            total=3,
            start=0.0,
            end=60.0,
            transcript_text="老师讲了一句话。",
            frames=[],
        )
        text = messages[-1]["content"][0]["text"]
        self.assertIn("第 1/3 段", text)
        self.assertIn("00:00 — 01:00", text)
        self.assertNotIn("__", text)

    def test_global_messages_build(self):
        messages = ln.build_global_messages(
            title="测试课",
            duration=180.0,
            subject="物理",
            audience="八年级",
            extra="",
            sections=[{"section_title": "A", "terms": [], "takeaways": [], "exam_points": []}],
        )
        text = messages[-1]["content"]
        self.assertIn("测试课", text)
        self.assertIn("1 段", text)
        self.assertNotIn("__", text)


class TestTranscriptWindow(unittest.TestCase):
    def test_window_picks_segments(self):
        transcript = {
            "segments": [
                {"start": 0, "end": 10, "text": "A"},
                {"start": 50, "end": 60, "text": "B"},
                {"start": 200, "end": 210, "text": "C"},
            ]
        }
        self.assertIn("B", ln.transcript_for_window(transcript, 45, 65))
        self.assertNotIn("C", ln.transcript_for_window(transcript, 45, 65))

    def test_plain_text_fallback(self):
        transcript = {"segments": [], "text": "x" * 5000}
        self.assertTrue(ln.transcript_for_window(transcript, 0, 100))


if __name__ == "__main__":
    unittest.main(verbosity=2)
