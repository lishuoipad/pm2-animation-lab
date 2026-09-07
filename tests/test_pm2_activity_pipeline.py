"""Independent arithmetic, scope, historical fault and publication checks."""
from __future__ import annotations
import copy
import unittest
from pathlib import Path
import os
from fractions import Fraction
from pm2_animation_lab.pm2_activity_clock import DeadlineClock, ClockError, validate_schedule, validate_groups, engine_groups
from pm2_animation_lab.pm2_activity_delivery_check import check_records, DeliveryError
from pm2_animation_lab.pm2_activity_scene_profiles import lower_course_loop
from pm2_animation_lab.pm2_activity_timeline import ParsedSource, SourceIdentityError
from pm2_animation_lab import pm2_activity_timeline as tl
def schedule(branches=("success",),weekday=1):
    return {"kind":"excerpt","start_weekday":weekday,"start_boundary":"first_present",
        "end_boundary":"after_last_exposure","days":[{"ordinal":i,"branch":b,
        "expected_state":None if b=="sunday" else 0,"random_values":[]} for i,b in enumerate(branches)]}


def observed():
    return {"scene":"TRG004","source_sha256":"a"*64,"size":[2,2],"schedule":schedule(),
        "groups":[{"ticks":[0],"start":[0,1],"end":[1,6],"rgb_sha256":"a"*64},
                  {"ticks":[1],"start":[1,6],"end":[7,20],"rgb_sha256":"b"*64}],
        "timing_tolerance":[0,1],"boundary_complete":True,"refresh_complete":False}


class ClockTests(unittest.TestCase):
    def test_timer_overlaps_work(self):
        c=DeadlineClock(60);c.start("a",8);c.work(Fraction(3,60));c.wait("a")
        self.assertEqual(c.now,Fraction(8,60))  # not (8+3)/60
    def test_overrun_does_not_run_clock_backwards(self):
        c=DeadlineClock(60);c.start("a",8);c.work(Fraction(11,60));c.wait("a")
        self.assertEqual(c.now,Fraction(11,60))
    def test_fractional_clock_has_no_accumulating_ms_rounding(self):
        c=DeadlineClock(60)
        for _ in range(600):c.start("a",1);c.wait("a")
        self.assertEqual(c.now,10)
    def test_wait_requires_started_timer(self):
        with self.assertRaises(ClockError):DeadlineClock(60).wait("missing")
    def test_explicit_boundary_gaps_and_final_hold(self):
        ticks=[{"call_index":i,"timer":{"logical_wait":8},"draw_operations":[1]} for i in range(2)]
        model={"kind":"engine_model","frequency":60,"background_ticks":1,"background_work":[0,1],
            "draw_work":[3,60],"copy_ticks":1,"copy_work":[0,1],"call_gaps":[[1,10]],
            "final_hold":[1,5],"evidence":[{"path":"independent_source","sha256":"a"*64}]}
        groups=engine_groups(ticks,model)
        self.assertEqual(Fraction(*groups[1]["start"]),Fraction(4,15))
        self.assertEqual(Fraction(*groups[1]["end"]),Fraction(7,15))
    def test_unknown_model_field_rejected(self):
        with self.assertRaises(ClockError):engine_groups([],{"duration":170})


class ScheduleTests(unittest.TestCase):
    def test_sunday_is_explicit_and_valid(self):
        self.assertEqual(len(validate_schedule(schedule(("success","sunday","success"),6))),3)
    def test_missing_sunday_rejected(self):
        with self.assertRaises(ClockError):validate_schedule(schedule(("success","success"),6))
    def test_reordered_days_rejected(self):
        s=schedule(("success","success"));s["days"][1]["ordinal"]=0
        with self.assertRaises(ClockError):validate_schedule(s)
    def test_unknown_branch_rejected(self):
        with self.assertRaises(ClockError):validate_schedule(schedule(("guess",)))
    def test_implicit_end_boundary_rejected(self):
        s=schedule();s["end_boundary"]="whenever"
        with self.assertRaises(ClockError):validate_schedule(s)
    def test_unknown_request_field_not_silently_ignored(self):
        s=schedule();s["fps"]=6
        with self.assertRaises(ClockError):validate_schedule(s)
    def test_merged_identical_exposures_keep_all_logical_ticks(self):
        validate_groups([{"ticks":[0,1],"start":[0,1],"end":[1,2]}],2)
    def test_hidden_tick_deletion_rejected(self):
        with self.assertRaises(ClockError):validate_groups([{"ticks":[0,2],"start":[0,1],"end":[1,2]}],3)
    def test_exposure_gap_rejected(self):
        with self.assertRaises(ClockError):validate_groups([{"ticks":[0],"start":[1,10],"end":[1,2]}],1)


class IndependentFaultTests(unittest.TestCase):
    def setUp(self):
        self.oracle=observed();self.candidate=copy.deepcopy(self.oracle);self.candidate["level"]="activity"
    def test_good_independent_expectation(self):check_records(self.candidate,self.oracle)
    def test_wrong_pattern_pixels_rejected(self):
        self.candidate["groups"][0]["rgb_sha256"]="c"*64
        with self.assertRaises(DeliveryError):check_records(self.candidate,self.oracle)
    def test_wrong_layer_pixels_rejected(self):
        self.candidate["groups"][1]["rgb_sha256"]="c"*64
        with self.assertRaises(DeliveryError):check_records(self.candidate,self.oracle)
    def test_phase_reset_rejected(self):
        self.candidate["groups"][1]["rgb_sha256"]="a"*64
        with self.assertRaises(DeliveryError):check_records(self.candidate,self.oracle)
    def test_constant_170_ms_rejected(self):
        self.candidate["groups"][0]["end"]=[17,100];self.candidate["groups"][1]["start"]=[17,100]
        with self.assertRaises(DeliveryError):check_records(self.candidate,self.oracle)
    def test_missing_exposure_rejected(self):
        self.candidate["groups"].pop()
        with self.assertRaises(DeliveryError):check_records(self.candidate,self.oracle)
    def test_last_hold_tampering_rejected(self):
        self.candidate["groups"][-1]["end"]=[45,1]
        with self.assertRaises(DeliveryError):check_records(self.candidate,self.oracle)
    def test_day_scope_changed_rejected(self):
        self.candidate["schedule"]["days"].append({"ordinal":1})
        with self.assertRaises(DeliveryError):check_records(self.candidate,self.oracle)
    def test_wrong_order_rejected(self):
        self.candidate["groups"].reverse()
        with self.assertRaises(DeliveryError):check_records(self.candidate,self.oracle)
    def test_unmeasured_boundary_cannot_be_activity(self):
        self.oracle["boundary_complete"]=False
        with self.assertRaises(DeliveryError):check_records(self.candidate,self.oracle)
    def test_stable_matches_cannot_be_refresh_pass(self):
        self.candidate["level"]="refresh"
        with self.assertRaises(DeliveryError):check_records(self.candidate,self.oracle)
    def test_changed_source_rejected(self):
        self.candidate["source_sha256"]="c"*64
        with self.assertRaises(DeliveryError):check_records(self.candidate,self.oracle)
    def test_tolerance_cannot_be_loosened_arbitrarily(self):
        self.oracle["timing_tolerance"]=[1,1]
        with self.assertRaises(DeliveryError):check_records(self.candidate,self.oracle)
    def test_unknown_course_source_fails_before_lowering(self):
        source=ParsedSource("TRG004","0"*64,"",(),{},{},{},{})
        with self.assertRaises(SourceIdentityError):lower_course_loop(source)


class FixedCourseIntegrationTests(unittest.TestCase):
    """Optional fixed-source checks; no original images or scripts in repo."""
    def setUp(self):
        self.source=Path(os.environ.get('PM2_SOURCE_ROOT','missing-external-source'))
        if not self.source.is_dir():self.skipTest("external fixed PM2 source unavailable")
    def sequence(self,scene,branches):
        tl.verify_source_checkout(self.source,tl.FIXED_SOURCE_COMMIT,scene)
        return tl.build_timeline_sequence(self.source/"KOSOTEXT"/(scene+".TXT"),scene,
            initialization_random_values=[],entry_state={"mode":"fresh_init_with_overrides","provenance":"explicit test entry","overrides":[]},
            calls=[{"branch":b,"expected_state":None if b=="sunday" else (2 if b=="mischief" else 1 if b=="failure" else 0),
                    "tick_count":5,"random_values":[]} for b in branches],source_commit=tl.FIXED_SOURCE_COMMIT)
    def test_sword_reserved_capacity_and_normal_cycle(self):
        s=self.sequence("TRG004",["success","success"])
        self.assertEqual(len(s["calls"][0]["entry_state"]["tracks"]),7)
        self.assertEqual([t["draw_operations"][-1]["pattern_number"] for t in s["ticks"]], [0,1,2,3,0,1,2,3,0,1])
    def test_sword_sunday_holds_phase_and_draws_background(self):
        s=self.sequence("TRG004",["success","sunday","success"])
        self.assertTrue(all(not t["draw_operations"] for t in s["ticks"][5:10]))
        self.assertEqual(s["ticks"][10]["draw_operations"][-1]["pattern_number"],1)
        self.assertEqual([t["draw_operations"][-2]["pattern_number"] for t in s["ticks"][:5]],[21,22,21,22,21])
    def test_martial_sunday_static_tracks_and_no_reset(self):
        s=self.sequence("TRG005",["success","sunday","success"])
        self.assertEqual(len(s["calls"][0]["entry_state"]["tracks"]),11)
        self.assertTrue(all(t["composite_track_order"]==[7,8,9,10] for t in s["ticks"][5:10]))
        self.assertEqual(s["ticks"][10]["draw_operations"][-1]["pattern_number"],1)
    def test_failure_and_mischief_source_selection(self):
        for scene in ("TRG004","TRG005"):
            s=self.sequence(scene,["failure","mischief","success"])
            self.assertEqual([c["selection"]["state"] for c in s["calls"]],[1,2,0])


if __name__=="__main__":unittest.main()
