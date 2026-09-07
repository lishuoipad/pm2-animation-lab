"""Exact presentation clock and explicit schedule; no guessed milliseconds."""
from __future__ import annotations
from fractions import Fraction


class ClockError(ValueError):
    pass


def rational(value):
    if (not isinstance(value, list) or len(value) != 2
            or any(type(x) is not int for x in value) or value[1] <= 0):
        raise ClockError("rational_pair_required")
    result = Fraction(*value)
    if result < 0:
        raise ClockError("negative_time")
    return result


def pair(value):
    return [value.numerator, value.denominator]


def exact_fields(value, fields, label):
    if not isinstance(value, dict) or set(value) != set(fields):
        raise ClockError("fields:" + label)


def validate_schedule(schedule):
    exact_fields(schedule, ("kind", "start_weekday", "days", "start_boundary", "end_boundary"), "schedule")
    if schedule["kind"] not in ("excerpt", "schedule"):
        raise ClockError("scope_kind")
    if type(schedule["start_weekday"]) is not int or not 0 <= schedule["start_weekday"] <= 6:
        raise ClockError("weekday")
    if schedule["start_boundary"] != "first_present" or schedule["end_boundary"] != "after_last_exposure":
        raise ClockError("unsupported_boundary")
    days = schedule["days"]
    if not isinstance(days, list) or not 1 <= len(days) <= 31:
        raise ClockError("days")
    for i, day in enumerate(days):
        exact_fields(day, ("ordinal", "branch", "expected_state", "random_values"), "day")
        if type(day["ordinal"]) is not int or day["ordinal"] != i:
            raise ClockError("day_gap_or_reorder")
        if day["branch"] not in ("success", "failure", "mischief", "sunday"):
            raise ClockError("unknown_day_branch")
        if (day["branch"] == "sunday") != ((schedule["start_weekday"] + i) % 7 == 0):
            raise ClockError("sunday_missing_or_wrong_day")
        if day["branch"] == "sunday" and day["expected_state"] is not None:
            raise ClockError("sunday_state")
        if not isinstance(day["random_values"], list):
            raise ClockError("random_tape")
    return days


class DeadlineClock:
    """A timer starts before work; WAIT consumes only its remaining duration."""
    def __init__(self, frequency):
        if type(frequency) is not int or frequency <= 0:
            raise ClockError("frequency")
        self.frequency = frequency
        self.now = Fraction(0)
        self.deadlines = {}

    def start(self, name, ticks):
        if type(ticks) is not int or ticks < 0:
            raise ClockError("timer_ticks")
        self.deadlines[name] = self.now + Fraction(ticks, self.frequency)

    def work(self, seconds):
        if seconds < 0:
            raise ClockError("work")
        self.now += seconds

    def wait(self, name):
        if name not in self.deadlines:
            raise ClockError("unstarted_timer")
        self.now = max(self.now, self.deadlines.pop(name))


def engine_groups(ticks, model):
    """Execute bounded engine phases with provenance-bound costs.

    This is an explicit model, not a claim of cycle-accurate DOS emulation.
    Model costs must be bound by the caller; delivery validation determines
    whether its predictions are eligible for an actual-activity claim.
    """
    exact_fields(model, ("kind", "frequency", "background_ticks", "background_work",
                         "draw_work", "copy_ticks", "copy_work", "call_gaps",
                         "final_hold", "evidence"), "engine_model")
    if model["kind"] != "engine_model" or not model["evidence"]:
        raise ClockError("model_evidence")
    clock = DeadlineClock(model["frequency"])
    starts = []
    previous_call = None
    gaps = model["call_gaps"]
    if not isinstance(gaps, list):
        raise ClockError("call_gaps")
    call_count = 1 + max(t["call_index"] for t in ticks)
    if len(gaps) != call_count - 1:
        raise ClockError("call_gap_coverage")
    for tick in ticks:
        call = tick["call_index"]
        if previous_call is not None and call != previous_call:
            clock.work(rational(gaps[call - 1]))
        clock.start("background", model["background_ticks"])
        clock.work(rational(model["background_work"]))
        clock.wait("background")
        clock.start("activity", tick["timer"]["logical_wait"])
        clock.work(len(tick["draw_operations"]) * rational(model["draw_work"]))
        clock.wait("activity")
        clock.start("copy", model["copy_ticks"])
        clock.work(rational(model["copy_work"]))
        starts.append(clock.now)
        clock.wait("copy")
        previous_call = call
    origin = starts[0]
    end = starts[-1] + rational(model["final_hold"])
    groups = [{"ticks": [i], "start": pair(start-origin),
               "end": pair((starts[i+1] if i+1 < len(starts) else end)-origin)}
              for i, start in enumerate(starts)]
    validate_groups(groups, len(ticks))
    return groups


def validate_groups(groups, tick_count):
    if not isinstance(groups, list) or not groups:
        raise ClockError("exposure_groups")
    flattened = []
    previous_end = Fraction(0)
    for group in groups:
        exact_fields(group, ("ticks", "start", "end"), "exposure_group")
        if not isinstance(group["ticks"], list) or not group["ticks"]:
            raise ClockError("group_ticks")
        if any(type(x) is not int for x in group["ticks"]):
            raise ClockError("group_tick_type")
        flattened.extend(group["ticks"])
        begin, end = rational(group["start"]), rational(group["end"])
        if begin != previous_end or end <= begin:
            raise ClockError("exposure_gap_overlap_or_zero")
        previous_end = end
    if flattened != list(range(tick_count)):
        raise ClockError("exposure_tick_coverage")
    return groups


def presentation_groups(ticks, clock):
    if clock.get("kind") == "engine_model":
        return engine_groups(ticks, clock)
    exact_fields(clock, ("kind", "groups", "evidence", "boundary_complete"), "capture_clock")
    if clock["kind"] != "capture_calibrated" or not clock["evidence"]:
        raise ClockError("clock_kind_or_evidence")
    if clock["boundary_complete"] is not True:
        raise ClockError("unmeasured_terminal_exposure")
    return validate_groups(clock["groups"], len(ticks))
