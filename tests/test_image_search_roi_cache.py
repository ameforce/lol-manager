from __future__ import annotations

import sys
import tempfile
from pathlib import Path
import unittest
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from lolmanager.core import image_search


def _reset_image_search_state() -> None:
    image_search._TEMPLATE_ROI_CACHE.clear()
    miss_cache = getattr(image_search, "_TEMPLATE_MISS_CACHE", None)
    if miss_cache is not None:
        miss_cache.clear()
    match_cache = getattr(image_search, "_TEMPLATE_MATCH_RESULT_CACHE", None)
    if match_cache is not None:
        match_cache.clear()
    if hasattr(image_search, "_LAST_GRAB_FRAME_TOKEN"):
        image_search._LAST_GRAB_FRAME_TOKEN = 0
    image_search._load_template_gray_cached.cache_clear()
    image_search._LAST_GRAB_RECT = None
    image_search._LAST_GRAB_AT_MONO = 0.0
    image_search._LAST_GRAB_BGRA = None
    image_search._LAST_GRAB_GRAY = None


def _template() -> np.ndarray:
    rng = np.random.default_rng(12345)
    return rng.integers(0, 255, size=(48, 48), dtype=np.uint8)


def _screen_with_template(template: np.ndarray, *, x: int, y: int) -> np.ndarray:
    rng = np.random.default_rng(67890)
    screen = rng.integers(0, 90, size=(180, 260), dtype=np.uint8)
    h, w = template.shape
    screen[y : y + h, x : x + w] = template
    return screen


def _screen_without_template() -> np.ndarray:
    rng = np.random.default_rng(24680)
    return rng.integers(0, 90, size=(180, 260), dtype=np.uint8)


def _bgra_from_gray(gray: np.ndarray) -> np.ndarray:
    bgr = np.repeat(gray[:, :, None], 3, axis=2)
    alpha = np.full(gray.shape + (1,), 255, dtype=np.uint8)
    return np.concatenate([bgr, alpha], axis=2)


class ImageSearchRoiCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        _reset_image_search_state()
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.template_path = Path(self.tmpdir.name) / "template.png"
        self.template_path.write_bytes(b"placeholder")

    def tearDown(self) -> None:
        _reset_image_search_state()

    def test_stale_cached_roi_is_cleared_after_full_frame_miss(self) -> None:
        template = _template()
        screen = _screen_with_template(template, x=50, y=60)
        rect = (0, 0, screen.shape[1], screen.shape[0])

        current_gray = {"value": screen}

        def fake_grab(_rect):
            gray = current_gray["value"]
            return _bgra_from_gray(gray), gray

        with (
            mock.patch.object(image_search, "_load_template_gray", return_value=template),
            mock.patch.object(image_search, "_grab_region_bgra_and_gray", side_effect=fake_grab),
        ):
            hit = image_search.find_template_center(rect, self.template_path, threshold=0.999)
            self.assertIsNotNone(hit)
            self.assertEqual(len(image_search._TEMPLATE_ROI_CACHE), 1)

            current_gray["value"] = _screen_without_template()
            miss = image_search.find_template_center(rect, self.template_path, threshold=0.999)

        self.assertIsNone(miss)
        self.assertEqual(image_search._TEMPLATE_ROI_CACHE, {})

    def test_recent_full_frame_miss_skips_repeated_match_in_same_cache_window(self) -> None:
        template = _template()
        screen = _screen_with_template(template, x=50, y=60)
        rect = (0, 0, screen.shape[1], screen.shape[0])

        current_gray = {"value": screen}
        match_calls = {"count": 0}
        original_match = image_search._match_template_maxloc

        def fake_grab(_rect):
            gray = current_gray["value"]
            return _bgra_from_gray(gray), gray

        def counted_match(search_gray, template_gray):
            match_calls["count"] += 1
            return original_match(search_gray, template_gray)

        with (
            mock.patch.object(image_search, "_load_template_gray", return_value=template),
            mock.patch.object(image_search, "_grab_region_bgra_and_gray", side_effect=fake_grab),
            mock.patch.object(image_search, "_match_template_maxloc", side_effect=counted_match),
        ):
            self.assertIsNotNone(
                image_search.find_template_center(rect, self.template_path, threshold=0.999)
            )
            current_gray["value"] = _screen_without_template()
            match_calls["count"] = 0

            self.assertIsNone(
                image_search.find_template_center(rect, self.template_path, threshold=0.999)
            )
            first_miss_calls = match_calls["count"]

            self.assertIsNone(
                image_search.find_template_center(rect, self.template_path, threshold=0.999)
            )

        self.assertEqual(first_miss_calls, 2)
        self.assertEqual(match_calls["count"], first_miss_calls)

    def test_repeated_cached_roi_hit_reuses_same_frame_match_result(self) -> None:
        template = _template()
        screen = _screen_with_template(template, x=50, y=60)
        rect = (0, 0, screen.shape[1], screen.shape[0])
        match_calls = {"count": 0}
        original_match = image_search._match_template_maxloc

        def counted_match(search_gray, template_gray):
            match_calls["count"] += 1
            return original_match(search_gray, template_gray)

        with (
            mock.patch.object(image_search, "_load_template_gray", return_value=template),
            mock.patch.object(
                image_search,
                "_grab_region_bgra_and_gray",
                return_value=(_bgra_from_gray(screen), screen),
            ),
            mock.patch.object(image_search, "_match_template_maxloc", side_effect=counted_match),
        ):
            self.assertIsNotNone(
                image_search.find_template_center(rect, self.template_path, threshold=0.999)
            )
            match_calls["count"] = 0

            self.assertIsNotNone(
                image_search.find_template_center(rect, self.template_path, threshold=0.999)
            )
            first_cached_hit_calls = match_calls["count"]

            self.assertIsNotNone(
                image_search.find_template_center(rect, self.template_path, threshold=0.999)
            )

        self.assertEqual(first_cached_hit_calls, 1)
        self.assertEqual(match_calls["count"], first_cached_hit_calls)

    def test_match_result_cache_does_not_reuse_stale_result_across_frames(self) -> None:
        template = _template()
        frame = _screen_without_template()
        rect = (0, 0, frame.shape[1], frame.shape[0])

        def fake_grab(_rect):
            return _bgra_from_gray(frame), frame

        with (
            mock.patch.object(image_search, "_load_template_gray", return_value=template),
            mock.patch.object(image_search, "_grab_region_bgra_and_gray", side_effect=fake_grab),
        ):
            self.assertIsNone(
                image_search.find_template_center(rect, self.template_path, threshold=0.999)
            )
            image_search._TEMPLATE_MISS_CACHE.clear()

            if hasattr(image_search, "_LAST_GRAB_FRAME_TOKEN"):
                image_search._LAST_GRAB_FRAME_TOKEN += 1
            frame[:, :] = _screen_with_template(template, x=50, y=60)

            self.assertIsNotNone(
                image_search.find_template_center(rect, self.template_path, threshold=0.999)
            )

    def test_roi_cache_evicts_oldest_entries_at_bound(self) -> None:
        limit = image_search._TEMPLATE_ROI_CACHE_MAX

        for idx in range(limit + 3):
            image_search._set_cached_roi(f"roi-{idx}", (idx, idx, idx + 1, idx + 1))

        self.assertLessEqual(len(image_search._TEMPLATE_ROI_CACHE), limit)
        self.assertNotIn("roi-0", image_search._TEMPLATE_ROI_CACHE)
        self.assertNotIn("roi-1", image_search._TEMPLATE_ROI_CACHE)
        self.assertNotIn("roi-2", image_search._TEMPLATE_ROI_CACHE)
        self.assertIn(f"roi-{limit + 2}", image_search._TEMPLATE_ROI_CACHE)

    def test_miss_cache_purges_expired_entries_and_evicts_oldest(self) -> None:
        limit = image_search._TEMPLATE_MISS_CACHE_MAX
        now = 100.0
        image_search._TEMPLATE_MISS_CACHE["expired"] = (
            now - image_search._GRAB_CACHE_MAX_AGE_SEC - 1.0
        )

        with mock.patch.object(image_search.time, "monotonic", return_value=now):
            image_search._set_recent_template_miss("fresh")

        self.assertNotIn("expired", image_search._TEMPLATE_MISS_CACHE)
        self.assertIn("fresh", image_search._TEMPLATE_MISS_CACHE)

        with mock.patch.object(image_search.time, "monotonic", return_value=now):
            for idx in range(limit + 2):
                image_search._set_recent_template_miss(f"miss-{idx}")

        self.assertLessEqual(len(image_search._TEMPLATE_MISS_CACHE), limit)
        self.assertNotIn("fresh", image_search._TEMPLATE_MISS_CACHE)
        self.assertNotIn("miss-0", image_search._TEMPLATE_MISS_CACHE)
        self.assertIn(f"miss-{limit + 1}", image_search._TEMPLATE_MISS_CACHE)

    def test_multi_template_search_captures_once(self) -> None:
        template = _template()
        screen = _screen_with_template(template, x=80, y=40)
        rect = (0, 0, screen.shape[1], screen.shape[0])
        template_paths = []
        for i in range(3):
            path = Path(self.tmpdir.name) / f"template-{i}.png"
            path.write_bytes(b"placeholder")
            template_paths.append(path)
        templates = [(f"tpl{i}", path) for i, path in enumerate(template_paths)]

        with (
            mock.patch.object(image_search, "_load_template_gray", return_value=template),
            mock.patch.object(
                image_search,
                "_grab_region_bgra_and_gray",
                return_value=(_bgra_from_gray(screen), screen),
            ) as grab,
        ):
            matches = image_search.find_template_matches_once(
                rect, templates, threshold=0.999
            )

        self.assertEqual(grab.call_count, 1)
        self.assertEqual(set(matches), {"tpl0", "tpl1", "tpl2"})

    def test_multi_template_search_reuses_duplicate_template_object(self) -> None:
        template = _template()
        screen = _screen_with_template(template, x=80, y=40)
        rect = (0, 0, screen.shape[1], screen.shape[0])
        template_paths = []
        for i in range(3):
            path = Path(self.tmpdir.name) / f"duplicate-template-{i}.png"
            path.write_bytes(b"placeholder")
            template_paths.append(path)
        templates = [(f"tpl{i}", path) for i, path in enumerate(template_paths)]
        match_calls = {"count": 0}
        original_match = image_search._match_template_maxloc

        def counted_match(search_gray, template_gray):
            match_calls["count"] += 1
            return original_match(search_gray, template_gray)

        with (
            mock.patch.object(image_search, "_load_template_gray", return_value=template),
            mock.patch.object(
                image_search,
                "_grab_region_bgra_and_gray",
                return_value=(_bgra_from_gray(screen), screen),
            ),
            mock.patch.object(image_search, "_match_template_maxloc", side_effect=counted_match),
        ):
            matches = image_search.find_template_matches_once(
                rect, templates, threshold=0.999
            )

        self.assertEqual(set(matches), {"tpl0", "tpl1", "tpl2"})
        self.assertEqual(match_calls["count"], 1)

    def test_gray_button_detection_uses_sampled_low_spread_pixels(self) -> None:
        grayish = np.full((32, 32, 3), 120, dtype=np.uint8)
        colorful = np.zeros((32, 32, 3), dtype=np.uint8)
        colorful[..., 0] = 30
        colorful[..., 1] = 170
        colorful[..., 2] = 240

        self.assertTrue(image_search.is_probably_disabled_gray_button(grayish))
        self.assertFalse(image_search.is_probably_disabled_gray_button(colorful))

    def test_direct_typing_fallback_escapes_pywinauto_key_syntax(self) -> None:
        send_calls: list[tuple[str, dict[str, object]]] = []

        def fake_send_keys(keys: str, **kwargs: object) -> None:
            send_calls.append((keys, kwargs))

        with (
            mock.patch.object(image_search, "find_template_center", return_value=(10, 20)),
            mock.patch.object(image_search.pyperclip, "copy"),
            mock.patch.object(image_search.pyperclip, "paste", return_value="not copied"),
            mock.patch.object(image_search.keyboard, "send_keys", side_effect=fake_send_keys),
        ):
            ok = image_search.search_and_act(
                (0, 0, 100, 100),
                self.template_path,
                keys="A B{TAB}+^%~()",
            )

        self.assertTrue(ok)
        self.assertEqual(send_calls[0][0], "^a{BACKSPACE}")
        self.assertEqual(send_calls[1][0], "A B{{}TAB{}}{+}{^}{%}{~}{(}{)}")
        self.assertTrue(send_calls[1][1]["with_spaces"])
        self.assertTrue(send_calls[1][1]["with_tabs"])
        self.assertTrue(send_calls[1][1]["with_newlines"])

    def test_click_relative_rejects_points_outside_window_rect(self) -> None:
        rect = (100, 200, 740, 680)
        invalid_points = ((-1, 0), (0, -1), (640, 0), (0, 480))

        for point in invalid_points:
            with self.subTest(point=point):
                with mock.patch.object(image_search, "click_screen") as click_screen:
                    with self.assertRaisesRegex(ValueError, "outside League window"):
                        image_search.click_relative(rect, point)

                click_screen.assert_not_called()

    def test_click_relative_accepts_last_point_inside_window_rect(self) -> None:
        with mock.patch.object(image_search, "click_screen") as click_screen:
            image_search.click_relative((100, 200, 740, 680), (639, 479))

        click_screen.assert_called_once_with((739, 679))


if __name__ == "__main__":
    unittest.main()
