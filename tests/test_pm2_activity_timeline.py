#!/usr/bin/env python3
"""Original-fixture tests for the restricted PM2 activity timeline parser."""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pm2_animation_lab.pm2_activity_timeline import (
    DeterminismError,
    MissingInputError,
    build_timeline,
    build_timeline_sequence,
    main,
    sequence_call_as_v1_timeline,
)


FIXTURE_COMMIT = "0" * 40


def _declarations(track_count: int, include_dog: bool) -> list[str]:
    result = [
        ".SLCANM",
        ".TIMELOP",
        ".TIMELOPMAX",
        ".TIMEWAIT1",
        f".ALOCX[{track_count}]",
        f".ALOCY[{track_count}]",
        f".ALOCC[{track_count}]",
        f".ALOCF[{track_count}]",
        f".ALOCCNT[{track_count}]",
        ".AFWORK=0",
        ".AFWORKCNT=0",
        ".AFMISS=0",
        ".AFMISSCNT=0",
    ]
    if include_dog:
        result.append(".DOG_COUNT=0")
    return result


def _track_lines(track_count: int, sentinel_track: int | None = None) -> list[str]:
    lines: list[str] = []
    for track in range(track_count):
        x = track + 2
        y = 40 - track
        values = [101 + track * 3, 102 + track * 3]
        if track == sentinel_track:
            values[0] = 0
        lines.extend(
            [
                f"ALOCX[{track}]={x}",
                f"ALOCY[{track}]={y}",
                f"ALOCC[{track}]=0",
                f"ALOCCNT[{track}]=2",
                f".FILM{track}[2]={values[0]},{values[1]}",
            ]
        )
    return lines


def _common_selector(job003: bool) -> list[str]:
    lines = [
        "*ANIME_WIDW",
        "IF ( AFMISSCNT = 0 )",
        "\tRANDAM(2)",
        "\tAFMISS = IRND - 1",
        "\tALOCC[2] = 0",
        "\tALOCC[3] = 0",
        "\tAFMISSCNT = 2",
        "\tIF ( SLCANM = 0 )( SLCANM = 1 )",
        "\t\tAFMISS = AFWORK",
        "IF ( AFWORKCNT = 0 )",
        "\tRANDAM(2)",
        "\tAFWORK = IRND - 1",
    ]
    if job003:
        lines.extend(["\tRANDAM(2)", "\tAFWORKCNT = IRND + 1"])
    else:
        lines.append("\tAFWORKCNT = 4")
    lines.extend(
        [
            "AFWORKCNT--",
            "IF ( SUCCESS_FLAG ! 0 )",
            "\tAFMISSCNT = 0",
            "\tIF ( AFWORK=0 ) ANMT001(0)",
            "\tIF ( AFWORK=1 ) ANMT001(1)",
            "\tGOTO ANIME_WIDW_SHOW",
            "IF ( S_HIKOUKA >= 10 )",
            "\tANMT001(4)",
            "\tGOTO ANIME_WIDW_SHOW",
            "AFMISSCNT--",
            "IF ( AFMISS=0 ) ANMT001(2)",
            "IF ( AFMISS=1 ) ANMT001(3)",
            "*ANIME_WIDW_SHOW",
            "RET",
        ]
    )
    return lines


def job003_fixture() -> str:
    lines = _declarations(13, False)
    lines.extend(
        [
            "*ENTRY",
            "RET",
            "*ANIME_INIT",
            "TIMEWAIT1=7",
            "TIMELOPMAX=3",
        ]
    )
    lines.extend(_track_lines(13))
    lines.append("RET")
    lines.extend(_common_selector(True))
    lines.extend(
        [
            "*ANMT001",
            "SLCANM=AX",
            "TIMELOP=TIMELOPMAX",
            "\tWWANIME(4,0)",
            "\tTIMER1(TIMEWAIT1)",
            "\tANIM_NUM(11) APUT(ALOCX[11],ALOCY[11],DX)",
            "\tANIM_NUM(12) APUT(ALOCX[12],ALOCY[12],DX)",
            "\tALOCC[11]++",
            "\tALOCC[12]++",
            "\tIF ( SLCANM = 1 )",
            "\t\tANIM_NUM(5) APUT(ALOCX[5],ALOCY[5],DX)",
            "\t\tANIM_NUM(7) APUT(ALOCX[7],ALOCY[7],DX)",
            "\t\tALOCC[5]++",
            "\t\tALOCC[7]++",
            "\tIF ( SLCANM = 3 )",
            "\t\tANIM_NUM(6) APUT(ALOCX[6],ALOCY[6],DX)",
            "\t\tANIM_NUM(7) APUT(ALOCX[7],ALOCY[7],DX)",
            "\t\tALOCC[6]++",
            "\t\tALOCC[7]++",
            "\tANIM_NUM(8) APUT(ALOCX[8],ALOCY[8],DX)",
            "\tALOCC[8]++",
            "\tANIM_NUM(SLCANM)",
            "\tIF ( SLCANM = 0 )",
            "\t\tIF ( DX = 3 )",
            "\t\t\tALOCX[0]=10",
            "\t\tIF ( DX >= 4 )",
            "\t\t\tALOCX[0]=11",
            "\t\tIF ( DX <= 2 )",
            "\t\t\tALOCX[0]=9",
            "\tAPUT(ALOCX[SLCANM],ALOCY[SLCANM],DX)",
            "\tALOCC[SLCANM]++",
            "\tIF ( SLCANM = 0 )( SLCANM = 2 )",
            "\t\tANIM_NUM(9) APUT(ALOCX[9],ALOCY[9],DX)",
            "\t\tALOCC[9]++",
            "\tIF ( SLCANM = 1 )( SLCANM = 3 )",
            "\t\tANIM_NUM(10) APUT(ALOCX[10],ALOCY[10],DX)",
            "\t\tALOCC[10]++",
            "\tWAIT1",
            "\tWWANIME(5,0)",
            "LOOP TIMELOP",
            "RET",
            "*ANMTSUNDAY",
            "TIMELOP=TIMELOPMAX",
            "\tWWANIME(4,0)",
            "\tTIMER1(TIMEWAIT1)",
            "\tANIM_NUM(11) APUT(ALOCX[11],ALOCY[11],DX)",
            "\tANIM_NUM(12) APUT(ALOCX[12],ALOCY[12],DX)",
            "\tALOCC[11]++",
            "\tALOCC[12]++",
            "\tWAIT1",
            "\tWWANIME(5,0)",
            "LOOP TIMELOP",
            "RET",
        ]
    )
    return "\r\n".join(lines) + "\r\n"


def job008_fixture() -> str:
    lines = _declarations(12, True)
    lines.extend(
        [
            "*ENTRY",
            "RET",
            "*ANIME_INIT",
            "TIMEWAIT1=9",
            "TIMELOPMAX=3",
        ]
    )
    lines.extend(_track_lines(12, sentinel_track=5))
    lines.extend(
        [
            "RANDAM(33)",
            "ALOCX[1]=IRND+1",
            "ALOCX[3]=ALOCX[1]",
            "RANDAM(2)",
            "IF ( IRND=1 )",
            "\tWORKS2_LEFT",
            "IF ( IRND=2 )",
            "\tWORKS2_RIGHT",
            "RET",
            "*WORKS2_RIGHT",
            "FILM1[0]=213",
            "FILM1[1]=214",
            "FILM3[0]=217",
            "FILM3[1]=218",
            "ALOCF[1]=0",
            "RET",
            "*WORKS2_LEFT",
            "FILM1[0]=215",
            "FILM1[1]=216",
            "FILM3[0]=219",
            "FILM3[1]=220",
            "ALOCF[1]=1",
            "RET",
            "*DOG_1N",
            "FILM9[0]=234",
            "FILM9[1]=235",
            "ALOCF[0]=0",
            "RET",
            "*DOG_2N",
            "FILM9[0]=236",
            "FILM9[1]=237",
            "ALOCF[0]=1",
            "RET",
        ]
    )
    lines.extend(_common_selector(False))
    lines.extend(
        [
            "*ANMT001",
            "SLCANM=AX",
            "TIMELOP=TIMELOPMAX",
            "\tWWANIME(4,0)",
            "\tTIMER1(TIMEWAIT1)",
            "\tANIM_NUM(9) APUT(ALOCX[9],ALOCY[9],DX)",
            "\tALOCC[9]++",
            "\tANIM_NUM(10) APUT(ALOCX[10],ALOCY[10],DX)",
            "\tALOCC[10]++",
            "\tDOG_COUNT++",
            "\tIF ( DOG_COUNT = 20 )",
            "\t\tDOG_2N",
            "\tIF ( DOG_COUNT = 40 )",
            "\t\tDOG_1N",
            "\t\tDOG_COUNT=0",
            "\tIF ( SLCANM=0 )",
            "\t\tANIM_NUM(6) APUT(ALOCX[6],ALOCY[6],DX) ALOCC[6]++",
            "\t\tANIM_NUM(5) APUT(ALOCX[5],ALOCY[5],DX) ALOCC[5]++",
            "\tIF ( SLCANM=2 )",
            "\t\tANIM_NUM(7) APUT(ALOCX[7],ALOCY[7],DX) ALOCC[7]++",
            "\tIF ( SLCANM=1 )( SLCANM=3 )",
            "\t\tANIM_NUM(8) APUT(ALOCX[8],ALOCY[8],DX) ALOCC[8]++",
            "\tANIM_NUM(SLCANM) APUT(ALOCX[SLCANM],ALOCY[SLCANM],DX)",
            "\tALOCC[SLCANM]++",
            "\tIF ( SLCANM = 1 )",
            "\t\tIF ( ALOCF[1] = 0 )",
            "\t\t\tIF ( ALOCX[1]>=34 )",
            "\t\t\t\tWORKS2_LEFT",
            "\t\t\tIF ( ALOCX[1]<34 )",
            "\t\t\t\tALOCX[1]++",
            "\t\tIF ( ALOCF[1] = 1 )",
            "\t\t\tIF ( ALOCX[1]<=3 )",
            "\t\t\t\tWORKS2_RIGHT",
            "\t\t\tIF ( ALOCX[1]>3 )",
            "\t\t\t\tALOCX[1]--",
            "\tALOCX[3]=ALOCX[1]",
            "\tWAIT1",
            "\tWWANIME(5,0)",
            "LOOP TIMELOP",
            "RET",
            "*ANMTSUNDAY",
            "TIMELOP=TIMELOPMAX",
            "\tWWANIME(4,0)",
            "\tTIMER1(TIMEWAIT1)",
            "\tWAIT1",
            "\tWWANIME(5,0)",
            "LOOP TIMELOP",
            "RET",
        ]
    )
    return "\n".join(lines) + "\n"


class ActivityTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.job003 = self.root / "JOB003.TXT"
        self.job008 = self.root / "JOB008.TXT"
        self.job003.write_text(job003_fixture(), encoding="ascii", newline="")
        self.job008.write_text(job008_fixture(), encoding="ascii", newline="")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def timeline(
        self,
        scene: str,
        *,
        branch: str | None,
        state: int | None,
        ticks: int | None,
        random_values: list[int] | None,
    ) -> dict:
        path = self.job003 if scene == "JOB003" else self.job008
        return build_timeline(
            path,
            scene,
            branch=branch,
            expected_state=state,
            tick_count=ticks,
            random_values=random_values,
            source_commit=FIXTURE_COMMIT,
        )

    @staticmethod
    def fresh_entry(*overrides: dict) -> dict:
        return {
            "mode": "fresh_init_with_overrides",
            "provenance": "original_fixture_test",
            "overrides": list(overrides),
        }

    def test_missing_branch_randoms_ticks_and_state_fail_closed(self) -> None:
        with self.assertRaises(MissingInputError):
            self.timeline("JOB003", branch=None, state=0, ticks=1, random_values=[1, 1, 1])
        with self.assertRaises(MissingInputError):
            self.timeline("JOB003", branch="success", state=0, ticks=1, random_values=None)
        with self.assertRaises(MissingInputError):
            self.timeline("JOB003", branch="success", state=0, ticks=None, random_values=[1, 1, 1])
        with self.assertRaises(MissingInputError):
            self.timeline("JOB003", branch="success", state=None, ticks=1, random_values=[1, 1, 1])

    def test_job003_source_selected_state_and_draw_order(self) -> None:
        result = self.timeline(
            "JOB003", branch="success", state=1, ticks=2, random_values=[1, 2, 1]
        )
        self.assertEqual(result["selection"]["state"], 1)
        self.assertEqual(result["parser_scope"]["source_default_tick_count"], 3)
        self.assertFalse(result["parser_scope"]["complete_source_loop"])
        first = result["ticks"][0]
        self.assertEqual(first["composite_track_order"], [11, 12, 5, 7, 8, 1, 10])
        self.assertEqual(first["draw_operations"][0]["film_value"], 134)
        self.assertEqual(first["draw_operations"][0]["pattern_number"], 133)
        self.assertEqual(first["draw_operations"][0]["author_coordinate"], {"x": 13, "y": 29})
        self.assertEqual(first["draw_operations"][0]["cursor"], {
            "before": 0,
            "used": 0,
            "after": 1,
            "wrapped_before_draw": False,
        })
        self.assertEqual(result["ticks"][1]["draw_operations"][0]["film_value"], 135)

    def test_job003_state_mismatch_and_unused_randoms_fail(self) -> None:
        with self.assertRaises(DeterminismError):
            self.timeline("JOB003", branch="success", state=0, ticks=1, random_values=[1, 2, 1])
        with self.assertRaises(DeterminismError):
            self.timeline("JOB003", branch="success", state=0, ticks=1, random_values=[1, 1, 1, 2])
        with self.assertRaises(MissingInputError):
            self.timeline("JOB003", branch="success", state=0, ticks=1, random_values=[1, 1])

    def test_repeat_is_identical_and_fixture_numbers_drive_output(self) -> None:
        first = self.timeline(
            "JOB003", branch="success", state=0, ticks=1, random_values=[1, 1, 1]
        )
        repeated = self.timeline(
            "JOB003", branch="success", state=0, ticks=1, random_values=[1, 1, 1]
        )
        self.assertEqual(first, repeated)

        variant_path = self.root / "JOB003_VARIANT.TXT"
        variant = job003_fixture().replace(".FILM11[2]=134,135", ".FILM11[2]=734,735")
        variant_path.write_text(variant, encoding="ascii", newline="")
        changed = build_timeline(
            variant_path,
            "JOB003",
            branch="success",
            expected_state=0,
            tick_count=1,
            random_values=[1, 1, 1],
            source_commit=FIXTURE_COMMIT,
        )
        self.assertEqual(first["ticks"][0]["draw_operations"][0]["film_value"], 134)
        self.assertEqual(changed["ticks"][0]["draw_operations"][0]["film_value"], 734)
        self.assertNotEqual(first["sources"][0]["sha256"], changed["sources"][0]["sha256"])

    def test_explicit_entry_cursor_override_controls_visible_track_phase(self) -> None:
        variant_path = self.root / "JOB003_PHASE.TXT"
        variant = (
            job003_fixture()
            .replace("ALOCCNT[8]=2", "ALOCCNT[8]=5")
            .replace(".FILM8[2]=125,126", ".FILM8[5]=133,133,132,133,132")
        )
        variant_path.write_text(variant, encoding="ascii", newline="")
        result = build_timeline(
            variant_path,
            "JOB003",
            branch="success",
            expected_state=0,
            tick_count=3,
            random_values=[1, 1, 1],
            source_commit=FIXTURE_COMMIT,
            entry_state=self.fresh_entry(
                {"name": "ALOCC", "index": 8, "value": 1}
            ),
        )
        track_eight = [
            next(draw for draw in tick["draw_operations"] if draw["track"] == 8)[
                "pattern_number"
            ]
            for tick in result["ticks"]
        ]
        self.assertEqual(track_eight, [132, 131, 132])
        self.assertEqual(
            result["explicit_inputs"]["entry_state"]["overrides"],
            [{"ordinal": 0, "name": "ALOCC", "index": 8, "value": 1}],
        )
        self.assertEqual(
            result["parser_scope"]["initialization"], "fresh_init_with_overrides"
        )

    def test_sequence_runs_init_once_and_inherits_call_state(self) -> None:
        result = build_timeline_sequence(
            self.job003,
            "JOB003",
            initialization_random_values=[],
            entry_state=self.fresh_entry(),
            calls=[
                {
                    "branch": "success",
                    "expected_state": 0,
                    "tick_count": 3,
                    "random_values": [1, 1, 1],
                },
                {
                    "branch": "success",
                    "expected_state": 0,
                    "tick_count": 3,
                    "random_values": [1],
                },
            ],
            source_commit=FIXTURE_COMMIT,
        )
        second_first = next(
            draw
            for draw in result["calls"][1]["ticks"][0]["draw_operations"]
            if draw["track"] == 8
        )
        self.assertEqual(second_first["cursor"]["before"], 1)
        self.assertEqual(second_first["film_value"], 126)
        self.assertEqual(result["calls"][1]["ticks"][0]["call_tick"], 0)
        self.assertEqual(result["calls"][1]["ticks"][0]["global_tick"], 3)
        self.assertEqual(len(result["ticks"]), 6)

    def test_sequence_requires_complete_calls_and_complete_entry_shape(self) -> None:
        with self.assertRaises(DeterminismError):
            build_timeline_sequence(
                self.job003,
                "JOB003",
                initialization_random_values=[],
                entry_state=self.fresh_entry(),
                calls=[
                    {
                        "branch": "success",
                        "expected_state": 0,
                        "tick_count": 1,
                        "random_values": [1, 1, 1],
                    }
                ],
                source_commit=FIXTURE_COMMIT,
            )
        with self.assertRaises(MissingInputError):
            build_timeline_sequence(
                self.job003,
                "JOB003",
                initialization_random_values=[],
                entry_state={"mode": "fresh_init_with_overrides", "overrides": []},
                calls=[],
                source_commit=FIXTURE_COMMIT,
            )
        with self.assertRaises(DeterminismError):
            build_timeline_sequence(
                self.job003,
                "JOB003",
                initialization_random_values=[],
                entry_state={
                    "mode": "complete_call_entry",
                    "provenance": "incomplete_original_fixture_snapshot",
                    "source_initialized_complete_state_sha256": "0" * 64,
                    "state": {"scalars": {}, "arrays": {}},
                },
                calls=[
                    {
                        "branch": "success",
                        "expected_state": 0,
                        "tick_count": 3,
                        "random_values": [1, 1, 1],
                    }
                ],
                source_commit=FIXTURE_COMMIT,
            )

    def test_complete_continuation_entry_reproduces_next_call(self) -> None:
        calls = [
            {
                "branch": "success",
                "expected_state": 0,
                "tick_count": 3,
                "random_values": [1, 1, 1],
            },
            {
                "branch": "success",
                "expected_state": 0,
                "tick_count": 3,
                "random_values": [1],
            },
        ]
        uninterrupted = build_timeline_sequence(
            self.job003,
            "JOB003",
            initialization_random_values=[],
            entry_state=self.fresh_entry(),
            calls=calls,
            source_commit=FIXTURE_COMMIT,
        )
        continuation = uninterrupted["calls"][0]["continuation_entry_state"]
        resumed = build_timeline_sequence(
            self.job003,
            "JOB003",
            initialization_random_values=[],
            entry_state=continuation,
            calls=[calls[1]],
            source_commit=FIXTURE_COMMIT,
        )
        uninterrupted_patterns = [
            [draw["pattern_number"] for draw in tick["draw_operations"]]
            for tick in uninterrupted["calls"][1]["ticks"]
        ]
        resumed_patterns = [
            [draw["pattern_number"] for draw in tick["draw_operations"]]
            for tick in resumed["calls"][0]["ticks"]
        ]
        self.assertEqual(resumed_patterns, uninterrupted_patterns)
        self.assertEqual(
            resumed["calls"][0]["final_state"],
            uninterrupted["calls"][1]["final_state"],
        )

        projected = sequence_call_as_v1_timeline(uninterrupted, 1)
        self.assertEqual(projected["schema_version"], "pm2_activity_timeline/v1")
        self.assertEqual([tick["tick"] for tick in projected["ticks"]], [0, 1, 2])
        self.assertNotIn("global_tick", projected["ticks"][0])
        self.assertEqual(
            projected["entry_complete_state_sha256"],
            uninterrupted["calls"][1]["entry_complete_state_sha256"],
        )

    def test_single_call_without_entry_remains_fresh_v1(self) -> None:
        result = self.timeline(
            "JOB003", branch="success", state=0, ticks=1, random_values=[1, 1, 1]
        )
        self.assertEqual(result["schema_version"], "pm2_activity_timeline/v1")
        self.assertEqual(result["parser_scope"]["initialization"], "fresh_ANIME_INIT")
        self.assertNotIn("entry_state", result["explicit_inputs"])

    def test_job008_sentinel_and_source_motion(self) -> None:
        state_zero = self.timeline(
            "JOB008", branch="success", state=0, ticks=1, random_values=[5, 1, 1, 1]
        )
        attempts = state_zero["ticks"][0]["draw_operations"]
        sentinel = next(draw for draw in attempts if draw["track"] == 5)
        self.assertEqual(sentinel["pattern_number"], -1)
        self.assertFalse(sentinel["emitted"])
        self.assertNotIn(5, state_zero["ticks"][0]["composite_track_order"])

        moving = self.timeline(
            "JOB008", branch="success", state=1, ticks=2, random_values=[33, 2, 1, 2]
        )
        first_state_draw = next(
            draw for draw in moving["ticks"][0]["draw_operations"] if draw["track"] == 1
        )
        second_state_draw = next(
            draw for draw in moving["ticks"][1]["draw_operations"] if draw["track"] == 1
        )
        self.assertEqual(first_state_draw["author_coordinate"]["x"], 34)
        self.assertEqual(second_state_draw["author_coordinate"]["x"], 33)
        self.assertEqual(
            first_state_draw["provenance"]["film_value"]["routine"], "WORKS2_RIGHT"
        )
        x_changes = [
            change
            for change in moving["ticks"][1]["position_changes"]
            if change["track"] == 1 and change["axis"] == "x"
        ]
        self.assertEqual(x_changes[-1]["delta"], -1)
        self.assertEqual(moving["final_state"]["tracks"][1]["author_x"], 32)

    def test_sunday_requires_no_state_and_emits_fixed_envelope(self) -> None:
        result = self.timeline(
            "JOB003", branch="sunday", state=None, ticks=3, random_values=[]
        )
        self.assertTrue(result["parser_scope"]["complete_source_loop"])
        self.assertEqual(result["selection"]["mode"], "explicit_sunday")
        self.assertTrue(all(tick["background_restore"]["opcode"] == 4 for tick in result["ticks"]))
        self.assertTrue(all(tick["frame_copy"]["opcode"] == 5 for tick in result["ticks"]))
        with self.assertRaises(DeterminismError):
            self.timeline("JOB003", branch="sunday", state=0, ticks=1, random_values=[])

    def test_output_is_redacted_structural_json(self) -> None:
        result = self.timeline(
            "JOB003", branch="failure", state=2, ticks=1, random_values=[2, 1, 2]
        )
        encoded = json.dumps(result, ensure_ascii=True, sort_keys=True)
        self.assertNotIn(".TXT", encoded)
        self.assertNotIn("J004", encoded)
        self.assertNotIn("comment", encoded.lower())
        self.assertRegex(result["sources"][0]["sha256"], r"^[0-9a-f]{64}$")
        for draw in result["ticks"][0]["draw_operations"]:
            self.assertIn("provenance", draw)
            self.assertIsInstance(draw["pattern_number"], int)

    def test_cli_publishes_new_output_and_never_overwrites(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            existing = root / "existing.json"
            existing.write_text("keep", encoding="ascii")
            created = root / "created.json"
            base_args = ["--source-root", str(root), "--scene", "JOB003"]
            fixture_timeline = {"schema_version": "fixture", "safe": True}
            with mock.patch(
                "pm2_animation_lab.pm2_activity_timeline.verify_source_checkout",
                return_value=FIXTURE_COMMIT,
            ), mock.patch(
                "pm2_animation_lab.pm2_activity_timeline.build_timeline",
                return_value=fixture_timeline,
            ):
                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    self.assertEqual(main(base_args + ["--output", str(existing)]), 2)
                self.assertEqual(existing.read_text(encoding="ascii"), "keep")
                self.assertIn("output_already_exists", stderr.getvalue())
                self.assertNotIn("Traceback", stderr.getvalue())

                self.assertEqual(main(base_args + ["--output", str(created)]), 0)
            self.assertEqual(
                json.loads(created.read_text(encoding="utf-8")), fixture_timeline
            )


if __name__ == "__main__":
    unittest.main()
