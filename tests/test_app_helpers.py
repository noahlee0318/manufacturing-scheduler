"""Tests for Streamlit-facing input parsing that do not launch the app."""

from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace

import pandas as pd


try:
    import streamlit  # noqa: F401
except ModuleNotFoundError:
    def _cache_data(*_args, **_kwargs):
        return lambda function: function

    sys.modules["streamlit"] = SimpleNamespace(
        cache_data=_cache_data,
        set_page_config=lambda **_kwargs: None,
    )

from app import MAX_SAVED_JOBS, parse_job_editor, theme_css  # noqa: E402


class JobEditorTests(unittest.TestCase):
    def test_job_limit_is_reported_before_save_or_schedule(self) -> None:
        rows = [
            {
                "Job ID": f"JOB-{index + 1:02d}",
                "Complete Part": "PART-001",
                "Quantity": 1,
                "Priority": 3,
                "Due Time": "",
                "Material Ready": True,
                "Notes": "",
            }
            for index in range(MAX_SAVED_JOBS + 1)
        ]

        jobs, errors = parse_job_editor(pd.DataFrame(rows))

        self.assertEqual(MAX_SAVED_JOBS + 1, len(jobs))
        self.assertIn(
            f"Plans support at most {MAX_SAVED_JOBS} jobs. "
            "Remove 1 row before saving or building the schedule.",
            errors,
        )

    def test_theme_css_has_distinct_accessible_color_modes(self) -> None:
        light = theme_css("light")
        dark = theme_css("dark")

        self.assertIn("color-scheme: light", light)
        self.assertIn("#f7f6fb", light)
        self.assertIn("color-scheme: dark", dark)
        self.assertIn("#0f0c18", dark)
        self.assertNotEqual(light, dark)


if __name__ == "__main__":
    unittest.main()
