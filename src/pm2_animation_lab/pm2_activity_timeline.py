#!/usr/bin/env python3
"""Deterministic, redacted timeline extraction for PM2 JOB003/JOB008.

The interpreter deliberately supports only the numeric subset used by the
ANIME_INIT, ANIME_WIDW, ANMT001 and ANMTSUNDAY routines in the two fixed
scripts.  It never emits source text, comments, asset names, or dialogue.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence


FIXED_SOURCE_COMMIT = "ec7bdef58357185fe5344973c156b857a5de2c1f"
from pm2_animation_lab.pm2_activity_scene_profiles import PROFILES
SUPPORTED_SCENES = tuple(PROFILES)
SUPPORTED_BRANCHES = ("success", "failure", "mischief", "sunday")
SCHEMA_VERSION = "pm2_activity_timeline/v1"
SEQUENCE_SCHEMA_VERSION = "pm2_activity_timeline_sequence/v1"
ENTRY_STATE_MODES = ("fresh_init_with_overrides", "complete_call_entry")


class TimelineError(RuntimeError):
    """Fail-closed parser error without source-text disclosure."""

    code = "TIMELINE_ERROR"

    def __init__(self, detail: str = "") -> None:
        super().__init__(f"{self.code}:{detail}" if detail else self.code)


class MissingInputError(TimelineError):
    code = "MISSING_EXPLICIT_INPUT"


class SourceIdentityError(TimelineError):
    code = "SOURCE_IDENTITY_MISMATCH"


class UnsupportedSyntaxError(TimelineError):
    code = "UNSUPPORTED_SOURCE_SYNTAX"


class DeterminismError(TimelineError):
    code = "DETERMINISM_INPUT_MISMATCH"


@dataclass(frozen=True)
class SourceLine:
    number: int
    text: str


@dataclass(frozen=True)
class Routine:
    name: str
    label_line: int
    lines: tuple[SourceLine, ...]

    @property
    def end_line(self) -> int:
        return self.lines[-1].number if self.lines else self.label_line


@dataclass
class ParsedSource:
    scene_id: str
    raw_sha256: str
    normalized_sha256: str
    lines: tuple[SourceLine, ...]
    routines: dict[str, Routine]
    scalar_defaults: dict[str, int]
    array_defaults: dict[str, list[int]]
    declaration_lines: dict[tuple[str, int | None], int]


@dataclass
class RuntimeState:
    scalars: dict[str, int]
    arrays: dict[str, list[int]]
    value_provenance: dict[tuple[str, int | None], tuple[str, int]]
    active_tracks: tuple[int, ...] | None = None
    array_aliases: dict[str, tuple[str, int]] = field(default_factory=dict)

    def clone_numeric_snapshot(self) -> dict[str, Any]:
        scalar_names = (
            "SLCANM",
            "TIMEWAIT1",
            "TIMELOPMAX",
            "AFWORK",
            "AFWORKCNT",
            "AFMISS",
            "AFMISSCNT",
            "DOG_COUNT",
        )
        scalars = {name: self.scalars.get(name, 0) for name in scalar_names}
        tracks: list[dict[str, Any]] = []
        count_array = self.arrays.get("ALOCCNT", [])
        for track_id, count in enumerate(count_array):
            film = self.arrays.get(f"FILM{track_id}", [])
            tracks.append(
                {
                    "track": track_id,
                    "author_x": self._array_value("ALOCX", track_id),
                    "author_y": self._array_value("ALOCY", track_id),
                    "cursor": self._array_value("ALOCC", track_id),
                    "count": count,
                    "flag": self._array_value("ALOCF", track_id),
                    "film_sha256": canonical_sha256(film),
                }
            )
        return {"scalars": scalars, "tracks": tracks}

    def clone_complete_numeric_state(self) -> dict[str, Any]:
        """Return every numeric value needed to resume at a call boundary."""

        return {
            "scalars": dict(self.scalars),
            "arrays": {name: list(values) for name, values in self.arrays.items()},
        }

    def _array_value(self, name: str, index: int) -> int:
        values = self.arrays.get(name, [])
        return values[index] if index < len(values) else 0


@dataclass
class RandomTape:
    values: list[int]
    source_ref: int = 0
    offset: int = 0
    receipts: list[dict[str, Any]] = field(default_factory=list)

    def consume(self, bound: int, routine: str, line: int) -> int:
        if self.offset >= len(self.values):
            raise MissingInputError(f"random:{self.offset}:bound:{bound}")
        value = self.values[self.offset]
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= bound:
            raise DeterminismError(f"random:{self.offset}:bound:{bound}:value:{value}")
        self.receipts.append(
            {
                "ordinal": self.offset,
                "bound": bound,
                "value": value,
                "provenance": provenance(self.source_ref, routine, line),
            }
        )
        self.offset += 1
        return value

    def require_exhausted(self) -> None:
        if self.offset != len(self.values):
            raise DeterminismError(f"unused_random_values:{len(self.values) - self.offset}")


@dataclass
class PatternSelection:
    track: int
    cursor_before: int
    cursor_used: int
    wrapped: bool
    film_value: int
    pattern_number: int
    anim_line: int
    film_routine: str
    film_line: int


@dataclass
class TickTrace:
    tick: int
    source_ref: int = 0
    background_restore: dict[str, Any] | None = None
    timer: dict[str, Any] | None = None
    wait: dict[str, Any] | None = None
    frame_copy: dict[str, Any] | None = None
    draw_operations: list[dict[str, Any]] = field(default_factory=list)
    cursor_changes: list[dict[str, Any]] = field(default_factory=list)
    position_changes: list[dict[str, Any]] = field(default_factory=list)
    state_changes: list[dict[str, Any]] = field(default_factory=list)
    operation_sequence: int = 0
    current_pattern: PatternSelection | None = None

    def next_sequence(self) -> int:
        value = self.operation_sequence
        self.operation_sequence += 1
        return value

    def finalize(self) -> dict[str, Any]:
        if self.background_restore is None or self.frame_copy is None or self.wait is None:
            raise UnsupportedSyntaxError(f"tick_envelope:{self.tick}")
        composite = [
            draw["track"] for draw in self.draw_operations if draw["emitted"]
        ]
        return {
            "tick": self.tick,
            "background_restore": self.background_restore,
            "timer": self.timer,
            "draw_operations": self.draw_operations,
            "composite_track_order": composite,
            "cursor_changes": self.cursor_changes,
            "position_changes": self.position_changes,
            "state_changes": self.state_changes,
            "wait": self.wait,
            "frame_copy": self.frame_copy,
        }


def canonical_sha256(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def provenance(source_ref: int, routine: str, line: int) -> dict[str, Any]:
    return {"source_ref": source_ref, "routine": routine, "line": line}


def _strip_comment(text: str) -> str:
    return text.split(";", 1)[0].rstrip().rstrip("\x1a")


def _indent(text: str) -> int:
    prefix = text[: len(text) - len(text.lstrip(" \t"))]
    return sum(4 if character == "\t" else 1 for character in prefix)


def _normalized_bytes(raw: bytes) -> bytes:
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def parse_source(source_path: Path, scene_id: str) -> ParsedSource:
    if scene_id not in SUPPORTED_SCENES:
        raise MissingInputError(f"scene:{scene_id}")
    try:
        raw = source_path.read_bytes()
    except OSError as error:
        raise SourceIdentityError(f"scene:{scene_id}:unavailable") from error
    decoded = raw.decode("cp932", errors="replace")
    lines = tuple(SourceLine(index, text) for index, text in enumerate(decoded.splitlines(), 1))
    raw_sha = hashlib.sha256(raw).hexdigest()
    normalized_sha = hashlib.sha256(_normalized_bytes(raw)).hexdigest()

    label_indices: list[tuple[int, str, int]] = []
    for offset, source_line in enumerate(lines):
        match = re.match(r"^\s*\*([A-Z][A-Z0-9_]*)", _strip_comment(source_line.text))
        if match:
            label_indices.append((offset, match.group(1), source_line.number))
    routines: dict[str, Routine] = {}
    for position, (offset, name, label_line) in enumerate(label_indices):
        end = label_indices[position + 1][0] if position + 1 < len(label_indices) else len(lines)
        routines[name] = Routine(name, label_line, lines[offset + 1 : end])

    for required in ("ANIME_INIT", "ANIME_WIDW", "ANMT001", "ANMTSUNDAY"):
        if required not in routines:
            raise UnsupportedSyntaxError(f"{scene_id}:routine:{required}")

    scalar_defaults: dict[str, int] = {}
    array_defaults: dict[str, list[int]] = {}
    declaration_lines: dict[tuple[str, int | None], int] = {}
    first_label_offset = label_indices[0][0] if label_indices else len(lines)
    allowed_scalars = {
        "SLCANM",
        "TIMELOP",
        "TIMELOPMAX",
        "TIMEWAIT1",
        "GIRX",
        "GIRY",
        "AFWORK",
        "AFWORKCNT",
        "AFMISS",
        "AFMISSCNT",
        "DOG_COUNT",
    }
    allowed_arrays = {"ALOCX", "ALOCY", "ALOCC", "ALOCF", "ALOCCNT"}
    declaration_re = re.compile(
        r"^\.([A-Z][A-Z0-9_]*)(?:\[(\d+)\])?(?:\s*=\s*(-?\d+))?\s*$"
    )
    for source_line in lines[:first_label_offset]:
        clean = _strip_comment(source_line.text).strip()
        match = declaration_re.match(clean)
        if not match:
            continue
        name, size_text, default_text = match.groups()
        if size_text is not None and name in allowed_arrays:
            size = int(size_text)
            array_defaults[name] = [0] * size
            declaration_lines[(name, None)] = source_line.number
        elif size_text is None and (name in allowed_scalars or raw_sha == PROFILES[scene_id]['source_sha256']):
            scalar_defaults[name] = int(default_text or 0)
            declaration_lines[(name, None)] = source_line.number

    parsed = ParsedSource(
        scene_id=scene_id,
        raw_sha256=raw_sha,
        normalized_sha256=normalized_sha,
        lines=lines,
        routines=routines,
        scalar_defaults=scalar_defaults,
        array_defaults=array_defaults,
        declaration_lines=declaration_lines,
    )
    if scene_id not in ('JOB003','JOB008'):
        from pm2_animation_lab.pm2_activity_scene_profiles import lower_fixed_source
        return lower_fixed_source(parsed)
    return parsed


class NumericEvaluator(ast.NodeVisitor):
    def __init__(self, state: RuntimeState) -> None:
        self.state = state

    def evaluate(self, expression: str) -> int:
        try:
            tree = ast.parse(expression.strip(), mode="eval")
        except SyntaxError as error:
            raise UnsupportedSyntaxError("numeric_expression") from error
        value = self.visit(tree.body)
        if isinstance(value, bool) or not isinstance(value, int):
            raise UnsupportedSyntaxError("non_integer_expression")
        return value

    def visit_Constant(self, node: ast.Constant) -> int:
        if isinstance(node.value, bool) or not isinstance(node.value, int):
            raise UnsupportedSyntaxError("constant")
        return node.value

    def visit_Name(self, node: ast.Name) -> int:
        if node.id not in self.state.scalars:
            raise UnsupportedSyntaxError(f"scalar:{node.id}")
        return self.state.scalars[node.id]

    def visit_Subscript(self, node: ast.Subscript) -> int:
        if not isinstance(node.value, ast.Name):
            raise UnsupportedSyntaxError("subscript")
        name = node.value.id
        index = self.visit(node.slice)
        values = self.state.arrays.get(name)
        if values is None or not 0 <= index < len(values):
            raise UnsupportedSyntaxError(f"array:{name}:{index}")
        return values[index]

    def visit_UnaryOp(self, node: ast.UnaryOp) -> int:
        value = self.visit(node.operand)
        if isinstance(node.op, ast.USub):
            return -value
        if isinstance(node.op, ast.UAdd):
            return value
        raise UnsupportedSyntaxError("unary")

    def visit_BinOp(self, node: ast.BinOp) -> int:
        left = self.visit(node.left)
        right = self.visit(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        if isinstance(node.op, (ast.Div, ast.FloorDiv)):
            if right == 0:
                raise UnsupportedSyntaxError("division_by_zero")
            return int(left / right)
        raise UnsupportedSyntaxError("binary")

    def generic_visit(self, node: ast.AST) -> int:
        raise UnsupportedSyntaxError(f"expression_node:{type(node).__name__}")


class RestrictedInterpreter:
    def __init__(
        self,
        source: ParsedSource,
        state: RuntimeState,
        random_tape: RandomTape,
        trace: TickTrace | None = None,
    ) -> None:
        self.source = source
        self.state = state
        self.random_tape = random_tape
        self.trace = trace
        self.selected_states: list[dict[str, Any]] = []

    def execute_routine(self, name: str, lines: Sequence[SourceLine] | None = None) -> None:
        routine = self.source.routines.get(name)
        if routine is None:
            raise UnsupportedSyntaxError(f"routine:{name}")
        self._execute_lines(name, tuple(lines) if lines is not None else routine.lines)

    def _execute_lines(self, routine: str, lines: Sequence[SourceLine]) -> None:
        conditions: list[tuple[int, bool]] = []
        for source_line in lines:
            clean_with_indent = _strip_comment(source_line.text)
            clean = clean_with_indent.strip()
            if not clean:
                continue
            indent = _indent(clean_with_indent)
            while conditions and indent <= conditions[-1][0]:
                conditions.pop()
            parent_active = all(active for _, active in conditions)
            if clean.startswith("IF"):
                groups, remainder = self._parse_if(clean, routine, source_line.number)
                local_active = parent_active and any(self._evaluate_condition(group) for group in groups)
                active = parent_active and local_active
                if remainder:
                    if active:
                        signal = self._execute_commands(remainder, routine, source_line.number)
                        if signal in {"RET", "GOTO"}:
                            return
                else:
                    conditions.append((indent, active))
                continue
            if not parent_active:
                continue
            signal = self._execute_commands(clean, routine, source_line.number)
            if signal in {"RET", "GOTO"}:
                return

    def _parse_if(self, clean: str, routine: str, line: int) -> tuple[list[str], str]:
        match = re.match(r"^IF\s*((?:\s*\([^)]*\))+)(?:\s+|$)(.*)$", clean)
        if not match:
            raise UnsupportedSyntaxError(f"{routine}:{line}:if")
        groups = re.findall(r"\(([^)]*)\)", match.group(1))
        if not groups:
            raise UnsupportedSyntaxError(f"{routine}:{line}:if_groups")
        return groups, match.group(2).strip()

    def _evaluate_condition(self, condition: str) -> bool:
        terms = re.split(r'\s+(?=[A-Z][A-Z0-9_]*(?:\[[^\]]+\])?\s*(?:>=|<=|!|=|>|<))', condition.strip())
        if len(terms) > 1:
            return all(self._evaluate_condition(term) for term in terms)
        match = re.fullmatch(r"\s*(.+?)\s*(>=|<=|!|=|>|<)\s*(.+?)\s*", condition)
        if not match:
            raise UnsupportedSyntaxError("condition")
        left = self._eval(match.group(1))
        right = self._eval(match.group(3))
        operator = match.group(2)
        return {
            "=": left == right,
            "!": left != right,
            ">=": left >= right,
            "<=": left <= right,
            ">": left > right,
            "<": left < right,
        }[operator]

    def _eval(self, expression: str) -> int:
        return NumericEvaluator(self.state).evaluate(expression)

    def _execute_commands(self, command_text: str, routine: str, line: int) -> str | None:
        remaining = command_text.strip()
        while remaining:
            if remaining.startswith('IF'):
                groups, rest = self._parse_if(remaining, routine, line)
                return self._execute_commands(rest, routine, line) if any(self._evaluate_condition(g) for g in groups) else None
            declaration = re.fullmatch(r'\.([A-Z][A-Z0-9_]*)\s*=\s*(-?\d+)', remaining)
            if declaration:
                self._set_target(declaration[1], int(declaration[2]), routine, line, 'local_declaration')
                return None
            compound = re.match(r'^([A-Z][A-Z0-9_]*(?:\[[^\]]+\])?)\s*([+-])=\s*(.+)$', remaining)
            if compound:
                target, operator, expression = compound.groups()
                self._set_target(target, self._get_target(target)+(1 if operator=='+' else -1)*self._eval(expression), routine, line, 'compound_assignment')
                return None
            if remaining == "RET":
                return "RET"
            goto_match = re.match(r"^GOTO\s+([A-Z][A-Z0-9_]*)\s*$", remaining)
            if goto_match:
                return "GOTO"
            film_match = re.match(
                r"^\.FILM(\d+)\[(\d+)\](?:\s*=\s*([0-9,\s-]+))?\s*$", remaining
            )
            if film_match:
                track = int(film_match.group(1))
                length = int(film_match.group(2))
                payload = film_match.group(3)
                values = [int(value) for value in re.findall(r"-?\d+", payload or "")]
                if payload is not None and len(values) != length:
                    fixed = self.source.raw_sha256 == PROFILES[self.source.scene_id]['source_sha256']
                    # BBDEFINE allocates the declared size then writes initializer
                    # words sequentially. These two audited cases never read the
                    # uninitialized tail / overwritten next-declaration word.
                    if fixed and (self.source.scene_id,track,length,len(values)) == ('TRG003',0,12,13):
                        values = values[:length]
                    elif fixed and (self.source.scene_id,track,length,len(values)) == ('JOB011',9,10,1):
                        values += [0]*(length-len(values))
                    else:
                        raise UnsupportedSyntaxError(f"{routine}:{line}:film_length")
                self.state.arrays[f"FILM{track}"] = values if payload is not None else [0] * length
                for index in range(length):
                    self.state.value_provenance[(f"FILM{track}", index)] = (routine, line)
                return None
            function_match = re.match(
                r"^(RANDAM|ANIM_NUM|APUT|WWANIME|TIMER1|ANMT001)\(([^()]*)\)", remaining
            )
            if function_match:
                name = function_match.group(1)
                arguments = [value.strip() for value in function_match.group(2).split(",") if value.strip()]
                self._execute_function(name, arguments, routine, line)
                remaining = remaining[function_match.end() :].strip()
                continue
            increment_match = re.match(
                r"^([A-Z][A-Z0-9_]*(?:\[[^\]]+\])?)(\+\+|--)", remaining
            )
            if increment_match:
                target, operator = increment_match.groups()
                old = self._get_target(target)
                self._set_target(target, old + (1 if operator == "++" else -1), routine, line, "increment")
                remaining = remaining[increment_match.end() :].strip()
                continue
            assignment_match = re.match(
                r"^([A-Z][A-Z0-9_]*(?:\[[^\]]+\])?)\s*=\s*(.+)$", remaining
            )
            if assignment_match:
                target, expression = assignment_match.groups()
                self._set_target(target, self._eval(expression), routine, line, "assignment")
                return None
            bare_match = re.match(r"^([A-Z][A-Z0-9_]*)\b", remaining)
            if bare_match:
                name = bare_match.group(1)
                if name == "WAIT1":
                    if self.trace is not None:
                        self.trace.wait = {
                            "sequence": self.trace.next_sequence(),
                            "provenance": provenance(0, routine, line),
                        }
                    remaining = remaining[bare_match.end() :].strip()
                    continue
                if name in self.source.routines:
                    before = self._state_digest()
                    self.execute_routine(name)
                    after = self._state_digest()
                    if self.trace is not None and before != after:
                        self.trace.state_changes.append(
                            {
                                "sequence": self.trace.next_sequence(),
                                "kind": "routine_state_change",
                                "before_sha256": before,
                                "after_sha256": after,
                                "provenance": provenance(0, routine, line),
                                "routine_provenance": provenance(
                                    0, name, self.source.routines[name].label_line
                                ),
                            }
                        )
                    remaining = remaining[bare_match.end() :].strip()
                    continue
            raise UnsupportedSyntaxError(f"{routine}:{line}")
        return None

    def _execute_function(
        self, name: str, arguments: list[str], routine: str, line: int
    ) -> None:
        if name == "RANDAM":
            if len(arguments) != 1:
                raise UnsupportedSyntaxError(f"{routine}:{line}:random_args")
            bound = self._eval(arguments[0])
            self.state.scalars["IRND"] = self.random_tape.consume(bound, routine, line)
            self.state.scalars['AX'] = self.state.scalars['IRND']
            self.state.value_provenance[("IRND", None)] = (routine, line)
            return
        if name == "ANMT001":
            if len(arguments) != 1:
                raise UnsupportedSyntaxError(f"{routine}:{line}:selection_args")
            selected = self._eval(arguments[0])
            self.selected_states.append(
                {"state": selected, "provenance": provenance(0, routine, line)}
            )
            return
        if self.trace is None:
            raise UnsupportedSyntaxError(f"{routine}:{line}:trace_command")
        if name == "WWANIME":
            values = [self._eval(argument) for argument in arguments]
            if values == [17,0,0,0,0,1] and self.source.scene_id in ('JOB006','JOB009'):
                # Separate bank-0 author-offset foreground operation; it is not
                # an actor animation number and does not advance an actor cursor.
                self.trace.draw_operations.append({
                    'execution_order':len(self.trace.draw_operations),'sequence':self.trace.next_sequence(),
                    'track':1000,'film_value':None,'pattern_number':1000,'emitted':True,
                    'author_coordinate':{'x':0,'y':0},'cursor':{'before':0,'used':0,'after':0,'wrapped_before_draw':False},
                    'displacement_from_previous_draw':None,'source_operation':{'opcode':17,'bank':0,'record':1},
                    'provenance':{'wwanime':provenance(0,routine,line)}})
                return
            if values == [4, 0]:
                if self.trace.background_restore is not None:
                    raise UnsupportedSyntaxError(f"{routine}:{line}:multiple_backgrounds")
                self.trace.background_restore = {
                    "sequence": self.trace.next_sequence(),
                    "opcode": 4,
                    "window": 0,
                    "provenance": provenance(0, routine, line),
                }
                return
            if values == [5, 0]:
                if self.trace.frame_copy is not None:
                    raise UnsupportedSyntaxError(f"{routine}:{line}:multiple_copies")
                self.trace.frame_copy = {
                    "sequence": self.trace.next_sequence(),
                    "opcode": 5,
                    "window": 0,
                    "provenance": provenance(0, routine, line),
                }
                return
            raise UnsupportedSyntaxError(f"{routine}:{line}:wwanime_opcode")
        if name == "TIMER1":
            if len(arguments) != 1:
                raise UnsupportedSyntaxError(f"{routine}:{line}:timer_args")
            self.trace.timer = {
                "sequence": self.trace.next_sequence(),
                "logical_wait": self._eval(arguments[0]),
                "provenance": provenance(0, routine, line),
            }
            return
        if name == "ANIM_NUM":
            if len(arguments) != 1:
                raise UnsupportedSyntaxError(f"{routine}:{line}:anim_args")
            track = self._eval(arguments[0])
            cursors = self.state.arrays.get("ALOCC", [])
            counts = self.state.arrays.get("ALOCCNT", [])
            film = self.state.arrays.get(f"FILM{track}")
            if f'FILM{track}' in self.state.array_aliases:
                alias, amount = self.state.array_aliases[f'FILM{track}']
                film = list(film or []) + self.state.arrays[alias][:amount]
            if film is None or not 0 <= track < len(cursors) or not 0 <= track < len(counts):
                raise UnsupportedSyntaxError(f"{routine}:{line}:track:{track}")
            count = counts[track]
            if count > len(film) or count <= 0:
                raise UnsupportedSyntaxError(f"{routine}:{line}:track_count:{track}")
            before = cursors[track]
            wrapped = before >= count
            if wrapped:
                self._set_array_value("ALOCC", track, 0, routine, line, "wrap_before_draw")
            used = self.state.arrays["ALOCC"][track]
            film_value = film[used]
            film_routine, film_line = self.state.value_provenance.get(
                (f"FILM{track}", used), ("UNKNOWN", 0)
            )
            self.state.scalars["DX"] = film_value - 1
            self.trace.current_pattern = PatternSelection(
                track=track,
                cursor_before=before,
                cursor_used=used,
                wrapped=wrapped,
                film_value=film_value,
                pattern_number=film_value - 1,
                anim_line=line,
                film_routine=film_routine,
                film_line=film_line,
            )
            return
        if name == "APUT":
            if len(arguments) != 3 or self.trace.current_pattern is None:
                raise UnsupportedSyntaxError(f"{routine}:{line}:aput_context")
            x, y, pattern = (self._eval(argument) for argument in arguments)
            current = self.trace.current_pattern
            if pattern != current.pattern_number:
                raise UnsupportedSyntaxError(f"{routine}:{line}:aput_pattern")
            emitted = pattern >= 0
            author_x_routine, author_x_line = self.state.value_provenance.get(
                ("ALOCX", current.track), ("UNKNOWN", 0)
            )
            author_y_routine, author_y_line = self.state.value_provenance.get(
                ("ALOCY", current.track), ("UNKNOWN", 0)
            )
            self.trace.draw_operations.append(
                {
                    "execution_order": len(self.trace.draw_operations),
                    "sequence": self.trace.next_sequence(),
                    "track": current.track,
                    "film_value": current.film_value,
                    "pattern_number": current.pattern_number,
                    "emitted": emitted,
                    "author_coordinate": {"x": x, "y": y},
                    "cursor": {
                        "before": current.cursor_before,
                        "used": current.cursor_used,
                        "after": current.cursor_used,
                        "wrapped_before_draw": current.wrapped,
                    },
                    "displacement_from_previous_draw": None,
                    "provenance": {
                        "anim_num": provenance(0, routine, current.anim_line),
                        "aput": provenance(0, routine, line),
                        "film_value": provenance(
                            0, current.film_routine, current.film_line
                        ),
                        "author_x": provenance(0, author_x_routine, author_x_line),
                        "author_y": provenance(0, author_y_routine, author_y_line),
                    },
                }
            )
            self.trace.current_pattern = None
            return
        raise UnsupportedSyntaxError(f"{routine}:{line}:function:{name}")

    def _target_parts(self, target: str) -> tuple[str, int | None]:
        match = re.fullmatch(r"([A-Z][A-Z0-9_]*)(?:\[([^\]]+)\])?", target.strip())
        if not match:
            raise UnsupportedSyntaxError("target")
        name, index_expression = match.groups()
        return name, self._eval(index_expression) if index_expression is not None else None

    def _get_target(self, target: str) -> int:
        name, index = self._target_parts(target)
        if index is None:
            if name not in self.state.scalars:
                raise UnsupportedSyntaxError(f"scalar:{name}")
            return self.state.scalars[name]
        values = self.state.arrays.get(name)
        if values is None or not 0 <= index < len(values):
            raise UnsupportedSyntaxError(f"array:{name}:{index}")
        return values[index]

    def _set_target(
        self, target: str, value: int, routine: str, line: int, reason: str
    ) -> None:
        name, index = self._target_parts(target)
        if index is None:
            old = self.state.scalars.get(name, 0)
            self.state.scalars[name] = value
            self.state.value_provenance[(name, None)] = (routine, line)
            if self.trace is not None and old != value and name in {"DOG_COUNT"}:
                self.trace.state_changes.append(
                    {
                        "sequence": self.trace.next_sequence(),
                        "target": name,
                        "before": old,
                        "after": value,
                        "provenance": provenance(0, routine, line),
                    }
                )
            return
        self._set_array_value(name, index, value, routine, line, reason)

    def _set_array_value(
        self, name: str, index: int, value: int, routine: str, line: int, reason: str
    ) -> None:
        values = self.state.arrays.get(name)
        if values is not None and index >= len(values) and name in self.state.array_aliases:
            alias, size = self.state.array_aliases[name]
            if index-len(values) < size:
                return self._set_array_value(alias,index-len(values),value,routine,line,'declared_array_alias_write')
        if values is None or not 0 <= index < len(values):
            raise UnsupportedSyntaxError(f"array:{name}:{index}")
        old = values[index]
        values[index] = value
        self.state.value_provenance[(name, index)] = (routine, line)
        if self.trace is None:
            return
        if name == "ALOCC":
            event = {
                "sequence": self.trace.next_sequence(),
                "track": index,
                "before": old,
                "after": value,
                "reason": reason,
                "provenance": provenance(0, routine, line),
            }
            self.trace.cursor_changes.append(event)
            for draw in reversed(self.trace.draw_operations):
                if draw["track"] == index:
                    draw["cursor"]["after"] = value
                    break
        elif name in {"ALOCX", "ALOCY"}:
            self.trace.position_changes.append(
                {
                    "sequence": self.trace.next_sequence(),
                    "track": index,
                    "axis": "x" if name == "ALOCX" else "y",
                    "before": old,
                    "after": value,
                    "delta": value - old,
                    "changed": old != value,
                    "provenance": provenance(0, routine, line),
                }
            )
        elif name.startswith("FILM") or name == "ALOCF":
            if old != value:
                self.trace.state_changes.append(
                    {
                        "sequence": self.trace.next_sequence(),
                        "target_sha256": canonical_sha256([name, index]),
                        "before": old,
                        "after": value,
                        "provenance": provenance(0, routine, line),
                    }
                )

    def _state_digest(self) -> str:
        return canonical_sha256(
            {"scalars": self.state.scalars, "arrays": self.state.arrays}
        )


def _new_runtime(source: ParsedSource) -> RuntimeState:
    scalars = dict(source.scalar_defaults)
    arrays = {name: list(values) for name, values in source.array_defaults.items()}
    scalars.setdefault("IRND", 0)
    scalars.setdefault("AX", 0)
    scalars.setdefault("DX", 0)
    scalars.setdefault("SUCCESS_FLAG", 0)
    scalars.setdefault("S_HIKOUKA", 0)
    value_provenance = {
        key: ("DECLARATION", line) for key, line in source.declaration_lines.items()
    }
    profile = PROFILES.get(source.scene_id, {})
    active = tuple(profile['active_tracks']) if source.raw_sha256 == profile.get('source_sha256') else None
    aliases = {'FILM0':('FILM2',2)} if source.scene_id=='JOB014' and active is not None else {}
    return RuntimeState(scalars, arrays, value_provenance, active, aliases)


def _is_plain_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_nonempty_string(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MissingInputError(field_name)
    return value.strip()


def _validate_runtime_call_boundary(runtime: RuntimeState) -> None:
    """Reject incomplete or internally inconsistent call-entry state."""

    if runtime.scalars.get("TIMELOPMAX", 0) <= 0:
        raise DeterminismError("entry_state:TIMELOPMAX")
    for name in ("AFWORKCNT", "AFMISSCNT", "DOG_COUNT"):
        if name in runtime.scalars and runtime.scalars[name] < 0:
            raise DeterminismError(f"entry_state:{name}")

    counts = runtime.arrays.get("ALOCCNT")
    cursors = runtime.arrays.get("ALOCC")
    xs = runtime.arrays.get("ALOCX")
    ys = runtime.arrays.get("ALOCY")
    flags = runtime.arrays.get("ALOCF")
    if not all(isinstance(values, list) for values in (counts, cursors, xs, ys, flags)):
        raise DeterminismError("entry_state:track_arrays")
    assert counts is not None and cursors is not None
    assert xs is not None and ys is not None and flags is not None
    if not len(counts) == len(cursors) == len(xs) == len(ys) == len(flags):
        raise DeterminismError("entry_state:track_array_lengths")
    for track, count in enumerate(counts):
        if runtime.active_tracks is not None and track not in runtime.active_tracks:
            if count != 0 or runtime.arrays.get(f'FILM{track}'):
                raise DeterminismError(f'entry_state:reserved_track:{track}')
            continue
        if not _is_plain_integer(count) or count <= 0:
            raise DeterminismError(f"entry_state:ALOCCNT:{track}")
        cursor = cursors[track]
        if not _is_plain_integer(cursor) or cursor < 0:
            raise DeterminismError(f"entry_state:ALOCC:{track}")
        film = runtime.arrays.get(f"FILM{track}")
        alias_length = runtime.array_aliases.get(f'FILM{track}',('',0))[1]
        if not isinstance(film, list) or len(film)+alias_length < count:
            raise DeterminismError(f"entry_state:FILM{track}")


def _replace_with_complete_entry_state(
    runtime: RuntimeState,
    state_value: Any,
) -> None:
    if not isinstance(state_value, Mapping) or set(state_value) != {"scalars", "arrays"}:
        raise MissingInputError("entry_state:state")
    scalars_value = state_value.get("scalars")
    arrays_value = state_value.get("arrays")
    if not isinstance(scalars_value, Mapping) or not isinstance(arrays_value, Mapping):
        raise MissingInputError("entry_state:complete_maps")
    if set(scalars_value) != set(runtime.scalars):
        raise DeterminismError("entry_state:complete_scalar_keys")
    if set(arrays_value) != set(runtime.arrays):
        raise DeterminismError("entry_state:complete_array_keys")

    scalars: dict[str, int] = {}
    for name, value in scalars_value.items():
        if not isinstance(name, str) or not _is_plain_integer(value):
            raise DeterminismError(f"entry_state:scalar:{name}")
        scalars[name] = value
    arrays: dict[str, list[int]] = {}
    for name, value in arrays_value.items():
        if not isinstance(name, str) or not isinstance(value, list):
            raise DeterminismError(f"entry_state:array:{name}")
        if len(value) != len(runtime.arrays[name]) or any(
            not _is_plain_integer(item) for item in value
        ):
            raise DeterminismError(f"entry_state:array_values:{name}")
        arrays[name] = list(value)

    runtime.scalars = scalars
    runtime.arrays = arrays
    runtime.value_provenance = {
        (name, None): ("COMPLETE_CALL_ENTRY", 0) for name in scalars
    }
    runtime.value_provenance.update(
        {
            (name, index): ("COMPLETE_CALL_ENTRY", 0)
            for name, values in arrays.items()
            for index in range(len(values))
        }
    )


def apply_call_entry_state(
    runtime: RuntimeState,
    entry_state: Mapping[str, Any],
    *,
    initialized_complete_state_sha256: str,
) -> dict[str, Any]:
    """Apply one explicit call-entry model without inferring hidden state."""

    if not isinstance(entry_state, Mapping):
        raise MissingInputError("entry_state")
    mode = entry_state.get("mode")
    if mode not in ENTRY_STATE_MODES:
        raise MissingInputError(f"entry_state:mode:{mode}")
    provenance_value = _require_nonempty_string(
        entry_state.get("provenance"), "entry_state:provenance"
    )
    before_sha256 = canonical_sha256(runtime.clone_complete_numeric_state())
    if before_sha256 != initialized_complete_state_sha256:
        raise DeterminismError("entry_state:initializer_hash")

    receipt: dict[str, Any] = {
        "mode": mode,
        "provenance": provenance_value,
        "source_initialized_complete_state_sha256": initialized_complete_state_sha256,
    }
    if mode == "fresh_init_with_overrides":
        if set(entry_state) != {"mode", "provenance", "overrides"}:
            raise DeterminismError("entry_state:fresh_fields")
        overrides = entry_state.get("overrides")
        if not isinstance(overrides, list):
            raise MissingInputError("entry_state:overrides")
        applied: list[dict[str, Any]] = []
        seen: set[tuple[str, int | None]] = set()
        for ordinal, override in enumerate(overrides):
            if not isinstance(override, Mapping) or set(override) != {
                "name",
                "index",
                "value",
            }:
                raise DeterminismError(f"entry_state:override_fields:{ordinal}")
            name = override.get("name")
            index = override.get("index")
            value = override.get("value")
            if not isinstance(name, str) or not _is_plain_integer(value):
                raise DeterminismError(f"entry_state:override_value:{ordinal}")
            if index is None:
                if name not in runtime.scalars:
                    raise DeterminismError(f"entry_state:scalar_name:{name}")
                target = (name, None)
                if target in seen:
                    raise DeterminismError(f"entry_state:duplicate:{name}")
                runtime.scalars[name] = value
            else:
                if not _is_plain_integer(index):
                    raise DeterminismError(f"entry_state:index:{ordinal}")
                values = runtime.arrays.get(name)
                if values is None or not 0 <= index < len(values):
                    raise DeterminismError(f"entry_state:array_target:{name}:{index}")
                target = (name, index)
                if target in seen:
                    raise DeterminismError(f"entry_state:duplicate:{name}:{index}")
                values[index] = value
            seen.add(target)
            runtime.value_provenance[target] = ("ENTRY_OVERRIDE", 0)
            applied.append(
                {"ordinal": ordinal, "name": name, "index": index, "value": value}
            )
        receipt["overrides"] = applied
    else:
        if set(entry_state) != {
            "mode",
            "provenance",
            "source_initialized_complete_state_sha256",
            "state",
        }:
            raise DeterminismError("entry_state:complete_fields")
        claimed_initializer_sha = entry_state.get(
            "source_initialized_complete_state_sha256"
        )
        if claimed_initializer_sha != initialized_complete_state_sha256:
            raise DeterminismError("entry_state:complete_initializer_hash")
        _replace_with_complete_entry_state(runtime, entry_state.get("state"))
        receipt["complete_state_sha256"] = canonical_sha256(
            runtime.clone_complete_numeric_state()
        )

    _validate_runtime_call_boundary(runtime)
    receipt["entry_complete_state_sha256"] = canonical_sha256(
        runtime.clone_complete_numeric_state()
    )
    return receipt


def make_complete_call_entry(
    runtime: RuntimeState,
    *,
    initialized_complete_state_sha256: str,
    provenance_value: str,
) -> dict[str, Any]:
    return {
        "mode": "complete_call_entry",
        "provenance": _require_nonempty_string(
            provenance_value, "entry_state:provenance"
        ),
        "source_initialized_complete_state_sha256": initialized_complete_state_sha256,
        "state": runtime.clone_complete_numeric_state(),
    }


def _loop_body(routine: Routine) -> tuple[SourceLine, ...]:
    start: int | None = None
    end: int | None = None
    for index, source_line in enumerate(routine.lines):
        clean = _strip_comment(source_line.text).strip()
        if start is None and re.fullmatch(r"WWANIME\(4\s*,\s*0\)", clean):
            start = index
        if start is not None and clean.startswith("LOOP"):
            end = index
            break
    if start is None or end is None or start >= end:
        raise UnsupportedSyntaxError(f"routine_loop:{routine.name}")
    return routine.lines[start:end]


def _branch_values(branch: str, scene_id: str = "") -> tuple[int, int]:
    if branch == "success":
        return 1, 0
    if branch == "failure":
        return 0, 0
    if branch == "mischief":
        return 0, 30 if scene_id.startswith('TRG') else 10
    raise MissingInputError(f"branch:{branch}")


def execute_call_edge(source, runtime, tape, routine, edge):
    """Execute author code outside the presentation loop exactly once per call."""
    first = next(i for i,l in enumerate(routine.lines) if _strip_comment(l.text).strip() == 'WWANIME(4,0)')
    last = next(i for i,l in enumerate(routine.lines[first:],first) if _strip_comment(l.text).strip().startswith('LOOP'))
    lines = routine.lines[:first] if edge == 'before' else routine.lines[last+1:]
    if edge == 'before':
        runtime.scalars['AX'] = runtime.scalars.get('SLCANM',0)
    RestrictedInterpreter(source, runtime, tape).execute_routine(routine.name, lines)


def _validate_requested_inputs(
    branch: str | None,
    expected_state: int | None,
    tick_count: int | None,
    random_values: Sequence[int] | None,
) -> None:
    if branch is None:
        raise MissingInputError("branch")
    if branch not in SUPPORTED_BRANCHES:
        raise MissingInputError(f"branch:{branch}")
    if tick_count is None:
        raise MissingInputError("tick_count")
    if isinstance(tick_count, bool) or not isinstance(tick_count, int) or tick_count <= 0:
        raise DeterminismError(f"tick_count:{tick_count}")
    if random_values is None:
        raise MissingInputError("random_values")
    if branch == "sunday" and expected_state is not None:
        raise DeterminismError(f"sunday_state:{expected_state}")
    if branch != "sunday" and expected_state is None:
        raise MissingInputError("expected_state")
    if expected_state is not None and expected_state not in range(5):
        raise DeterminismError(f"expected_state:{expected_state}")


def build_timeline(
    source_path: Path,
    scene_id: str,
    *,
    branch: str | None,
    expected_state: int | None,
    tick_count: int | None,
    random_values: Sequence[int] | None,
    source_commit: str,
    entry_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one deterministic activity-call timeline.

    Omitting ``entry_state`` retains the original fresh-initialization v1
    behavior.  A supplied entry state is always explicit and fail-closed; no
    hidden cursor, counter, random value, or prior call is inferred from a
    visible frame.
    """

    _validate_requested_inputs(branch, expected_state, tick_count, random_values)
    assert branch is not None
    assert tick_count is not None
    assert random_values is not None
    source = parse_source(source_path, scene_id)
    runtime = _new_runtime(source)
    tape = RandomTape(list(random_values))
    initializer = RestrictedInterpreter(source, runtime, tape)
    initializer.execute_routine("ANIME_INIT")
    initialized_snapshot = runtime.clone_numeric_snapshot()
    initialized_complete_state_sha256 = canonical_sha256(
        runtime.clone_complete_numeric_state()
    )
    entry_receipt = None
    if entry_state is not None:
        entry_receipt = apply_call_entry_state(
            runtime,
            entry_state,
            initialized_complete_state_sha256=initialized_complete_state_sha256,
        )

    selection_receipt: dict[str, Any]
    if branch == "sunday":
        selected_state = None
        selection_receipt = {
            "mode": "explicit_sunday",
            "state": None,
            "provenance": provenance(
                0, "ANMTSUNDAY", source.routines["ANMTSUNDAY"].label_line
            ),
        }
        body_routine = source.routines["ANMTSUNDAY"]
    else:
        success_flag, hiko = _branch_values(branch, scene_id)
        runtime.scalars["SUCCESS_FLAG"] = success_flag
        runtime.scalars["S_HIKOUKA"] = hiko
        if scene_id.startswith('TRG'):
            runtime.scalars["S_BYOUKI"] = 0
        selector = RestrictedInterpreter(source, runtime, tape)
        selector.execute_routine("ANIME_WIDW")
        states = selector.selected_states
        if len(states) != 1:
            raise UnsupportedSyntaxError(f"selection_count:{len(states)}")
        selected_state = states[0]["state"]
        if selected_state != expected_state:
            raise DeterminismError(
                f"selected_state:{selected_state}:expected:{expected_state}"
            )
        runtime.scalars["SLCANM"] = selected_state
        selection_receipt = {
            "mode": "source_branch_execution",
            "state": selected_state,
            "success_flag": success_flag,
            "hiko_branch_value": hiko,
            "provenance": states[0]["provenance"],
        }
        body_routine = source.routines["ANMT001"]

    source_tick_count = runtime.scalars.get("TIMELOPMAX", 0)
    if source_tick_count <= 0 or tick_count > source_tick_count:
        raise DeterminismError(
            f"tick_count:{tick_count}:source_tick_count:{source_tick_count}"
        )

    body = _loop_body(body_routine)
    execute_call_edge(source, runtime, tape, body_routine, 'before')
    tick_results: list[dict[str, Any]] = []
    previous_coordinates: dict[int, tuple[int, int]] = {}
    for tick in range(tick_count):
        trace = TickTrace(tick)
        RestrictedInterpreter(source, runtime, tape, trace).execute_routine(
            body_routine.name, body
        )
        result = trace.finalize()
        for draw in result["draw_operations"]:
            coordinate = draw["author_coordinate"]
            current = (coordinate["x"], coordinate["y"])
            previous = previous_coordinates.get(draw["track"])
            draw["displacement_from_previous_draw"] = (
                {"known": False, "dx": 0, "dy": 0}
                if previous is None
                else {
                    "known": True,
                    "dx": current[0] - previous[0],
                    "dy": current[1] - previous[1],
                }
            )
            previous_coordinates[draw["track"]] = current
        tick_results.append(result)

    execute_call_edge(source, runtime, tape, body_routine, 'after')
    tape.require_exhausted()
    final_snapshot = runtime.clone_numeric_snapshot()
    routine_names = ("ANIME_INIT", "ANIME_WIDW", body_routine.name)
    routine_spans = {
        name: {
            "label_line": source.routines[name].label_line,
            "end_line": source.routines[name].end_line,
        }
        for name in dict.fromkeys(routine_names)
    }
    explicit_inputs: dict[str, Any] = {
        "branch": branch,
        "expected_state": expected_state,
        "tick_count": tick_count,
        "random_values": list(random_values),
        "provenance": "caller_explicit",
    }
    if entry_receipt is not None:
        explicit_inputs["entry_state"] = entry_receipt
    result = {
        "schema_version": SCHEMA_VERSION,
        "scene": {
            "scene_id": scene_id,
            "source_commit": source_commit,
            "source_ref": 0,
        },
        "sources": [
            {
                "source_ref": 0,
                "sha256": source.raw_sha256,
                "normalized_sha256": source.normalized_sha256,
            }
        ],
        "parser_scope": {
            "grammar": "restricted_numeric_activity_v1",
            "initialization": (
                "fresh_ANIME_INIT"
                if entry_receipt is None
                else entry_receipt["mode"]
            ),
            "selection": "single_ANIME_WIDW_or_ANMTSUNDAY",
            "body": "ANMT001_or_ANMTSUNDAY_prefix",
            "routine_spans": routine_spans,
            "source_default_tick_count": source_tick_count,
            "requested_tick_count": tick_count,
            "complete_source_loop": tick_count == source_tick_count,
        },
        "explicit_inputs": explicit_inputs,
        "random_receipts": tape.receipts,
        "initialized_state_sha256": canonical_sha256(initialized_snapshot),
        "selection": selection_receipt,
        "ticks": tick_results,
        "final_state": final_snapshot,
        "final_state_sha256": canonical_sha256(final_snapshot),
    }
    if entry_receipt is not None:
        result["initialized_complete_state_sha256"] = initialized_complete_state_sha256
        result["entry_complete_state_sha256"] = entry_receipt[
            "entry_complete_state_sha256"
        ]
    return result


def build_timeline_sequence(
    source_path: Path,
    scene_id: str,
    *,
    initialization_random_values: Sequence[int] | None,
    entry_state: Mapping[str, Any] | None,
    calls: Sequence[Mapping[str, Any]] | None,
    source_commit: str,
) -> dict[str, Any]:
    """Execute several complete activity calls over one shared runtime.

    ``ANIME_INIT`` runs exactly once.  Every later call has its own explicit
    branch, expected state, tick count, and random tape.  Calls are required to
    cover the complete source loop so a later call can never inherit a state
    that silently skipped un-emitted ticks.
    """

    if initialization_random_values is None:
        raise MissingInputError("initialization_random_values")
    if entry_state is None:
        raise MissingInputError("entry_state")
    if calls is None or not isinstance(calls, Sequence) or isinstance(calls, (str, bytes)):
        raise MissingInputError("calls")
    if not calls:
        raise MissingInputError("calls:empty")

    source = parse_source(source_path, scene_id)
    runtime = _new_runtime(source)
    initialization_tape = RandomTape(list(initialization_random_values))
    RestrictedInterpreter(source, runtime, initialization_tape).execute_routine(
        "ANIME_INIT"
    )
    initialization_tape.require_exhausted()
    initialized_snapshot = runtime.clone_numeric_snapshot()
    initialized_complete_state_sha256 = canonical_sha256(
        runtime.clone_complete_numeric_state()
    )
    entry_receipt = apply_call_entry_state(
        runtime,
        entry_state,
        initialized_complete_state_sha256=initialized_complete_state_sha256,
    )

    call_results: list[dict[str, Any]] = []
    flat_ticks: list[dict[str, Any]] = []
    previous_coordinates: dict[int, tuple[int, int]] = {}
    routine_names: list[str] = ["ANIME_INIT"]
    explicit_calls: list[dict[str, Any]] = []
    global_tick = 0

    for call_index, call in enumerate(calls):
        if not isinstance(call, Mapping) or set(call) != {
            "branch",
            "expected_state",
            "tick_count",
            "random_values",
        }:
            raise DeterminismError(f"call_fields:{call_index}")
        branch = call.get("branch")
        expected_state = call.get("expected_state")
        tick_count = call.get("tick_count")
        random_values = call.get("random_values")
        intro = branch == 'hunting_intro'
        grave_event = branch == 'graveyard_event'
        special = intro or grave_event
        if intro and (scene_id != 'JOB009' or call_index != 0):
            raise DeterminismError('hunting_intro_requires_JOB009_first_call')
        if grave_event and (scene_id != 'JOB010' or call_index != len(calls)-1):
            raise DeterminismError('graveyard_event_requires_JOB010_last_call')
        _validate_requested_inputs('sunday' if special else branch, expected_state, tick_count, random_values)
        if grave_event and tick_count > 128:
            raise DeterminismError('graveyard_event_bounded_ticks')
        assert isinstance(branch, str)
        assert isinstance(tick_count, int)
        assert isinstance(random_values, Sequence)
        if isinstance(random_values, (str, bytes)):
            raise DeterminismError(f"call_random_values:{call_index}")

        call_entry_snapshot = runtime.clone_numeric_snapshot()
        call_entry_complete_sha256 = canonical_sha256(
            runtime.clone_complete_numeric_state()
        )
        tape = RandomTape(list(random_values))
        selection_receipt: dict[str, Any]
        if special:
            selected_state = None
            body_routine = source.routines['ANMT_INTRO' if intro else 'ANMT_MOOOON']
            selection_receipt = {'mode':'explicit_source_special_phase','state':None,
                'provenance':provenance(0,body_routine.name,body_routine.label_line),
                'trigger_source_line':117 if intro else 214,
                'trigger_claim':'explicit research request; no claim that native event flag is set'}
        elif branch == "sunday":
            selected_state = None
            selection_receipt = {
                "mode": "explicit_sunday",
                "state": None,
                "provenance": provenance(
                    0, "ANMTSUNDAY", source.routines["ANMTSUNDAY"].label_line
                ),
            }
            body_routine = source.routines["ANMTSUNDAY"]
        else:
            success_flag, hiko = _branch_values(branch, scene_id)
            runtime.scalars["SUCCESS_FLAG"] = success_flag
            runtime.scalars["S_HIKOUKA"] = hiko
            if scene_id.startswith('TRG'):
                runtime.scalars["S_BYOUKI"] = 0
            selector = RestrictedInterpreter(source, runtime, tape)
            selector.execute_routine("ANIME_WIDW")
            states = selector.selected_states
            if len(states) != 1:
                raise UnsupportedSyntaxError(
                    f"call:{call_index}:selection_count:{len(states)}"
                )
            selected_state = states[0]["state"]
            if selected_state != expected_state:
                raise DeterminismError(
                    f"call:{call_index}:selected_state:{selected_state}:expected:{expected_state}"
                )
            runtime.scalars["SLCANM"] = selected_state
            selection_receipt = {
                "mode": "source_branch_execution",
                "state": selected_state,
                "success_flag": success_flag,
                "hiko_branch_value": hiko,
                "provenance": states[0]["provenance"],
            }
            body_routine = source.routines["ANMT001"]

        source_tick_count = 12 if intro else 45 if grave_event else runtime.scalars.get("TIMELOPMAX", 0)
        if source_tick_count <= 0 or (not grave_event and tick_count != source_tick_count):
            raise DeterminismError(
                f"call:{call_index}:complete_tick_count_required:{tick_count}:source:{source_tick_count}"
            )

        body = _loop_body(body_routine)
        execute_call_edge(source, runtime, tape, body_routine, 'before')
        call_ticks: list[dict[str, Any]] = []
        call_tick_start = global_tick
        for call_tick in range(tick_count):
            if grave_event and runtime.scalars['TIMELOP'] <= 0:
                raise DeterminismError('graveyard_event_extra_ticks')
            trace = TickTrace(global_tick)
            RestrictedInterpreter(source, runtime, tape, trace).execute_routine(
                body_routine.name, body
            )
            tick_result = trace.finalize()
            tick_result["call_index"] = call_index
            tick_result["call_tick"] = call_tick
            tick_result["global_tick"] = global_tick
            for draw in tick_result["draw_operations"]:
                coordinate = draw["author_coordinate"]
                current = (coordinate["x"], coordinate["y"])
                previous = previous_coordinates.get(draw["track"])
                draw["displacement_from_previous_draw"] = (
                    {"known": False, "dx": 0, "dy": 0}
                    if previous is None
                    else {
                        "known": True,
                        "dx": current[0] - previous[0],
                        "dy": current[1] - previous[1],
                    }
                )
                previous_coordinates[draw["track"]] = current
            call_ticks.append(tick_result)
            flat_ticks.append(tick_result)
            global_tick += 1
            if grave_event:
                runtime.scalars['TIMELOP'] -= 1

        if grave_event and runtime.scalars['TIMELOP'] != 0:
            raise DeterminismError('graveyard_event_incomplete_loop')

        execute_call_edge(source, runtime, tape, body_routine, 'after')
        tape.require_exhausted()
        call_final_snapshot = runtime.clone_numeric_snapshot()
        continuation = make_complete_call_entry(
            runtime,
            initialized_complete_state_sha256=initialized_complete_state_sha256,
            provenance_value=f"timeline_sequence_call_{call_index}_final_state",
        )
        call_results.append(
            {
                "call_index": call_index,
                "global_tick_start": call_tick_start,
                "global_tick_end_exclusive": global_tick,
                "source_default_tick_count": source_tick_count,
                "entry_state": call_entry_snapshot,
                "entry_complete_state_sha256": call_entry_complete_sha256,
                "selection": selection_receipt,
                "random_receipts": tape.receipts,
                "ticks": call_ticks,
                "final_state": call_final_snapshot,
                "final_state_sha256": canonical_sha256(call_final_snapshot),
                "continuation_entry_state": continuation,
            }
        )
        explicit_calls.append(
            {
                "call_index": call_index,
                "branch": branch,
                "expected_state": expected_state,
                "tick_count": tick_count,
                "random_values": list(random_values),
            }
        )
        for name in ("ANIME_WIDW" if branch not in ("sunday",'hunting_intro','graveyard_event') else None, body_routine.name):
            if name is not None and name not in routine_names:
                routine_names.append(name)

    final_snapshot = runtime.clone_numeric_snapshot()
    routine_spans = {
        name: {
            "label_line": source.routines[name].label_line,
            "end_line": source.routines[name].end_line,
        }
        for name in routine_names
    }
    return {
        "schema_version": SEQUENCE_SCHEMA_VERSION,
        "scene": {
            "scene_id": scene_id,
            "source_commit": source_commit,
            "source_ref": 0,
        },
        "sources": [
            {
                "source_ref": 0,
                "sha256": source.raw_sha256,
                "normalized_sha256": source.normalized_sha256,
            }
        ],
        "parser_scope": {
            "grammar": "restricted_numeric_activity_v1",
            "initialization": "single_ANIME_INIT_then_shared_runtime",
            "selection": "explicit_per_call_ANIME_WIDW_or_ANMTSUNDAY",
            "body": "complete_ANMT001_or_ANMTSUNDAY_prefix_per_call",
            "call_count": len(call_results),
            "global_tick_count": global_tick,
            "routine_spans": routine_spans,
            "hidden_state_inference": "forbidden",
        },
        "explicit_inputs": {
            "initialization_random_values": list(initialization_random_values),
            "entry_state": entry_receipt,
            "calls": explicit_calls,
            "provenance": "caller_explicit",
        },
        "initialization_random_receipts": initialization_tape.receipts,
        "initialized_state_sha256": canonical_sha256(initialized_snapshot),
        "initialized_complete_state_sha256": initialized_complete_state_sha256,
        "entry_complete_state_sha256": entry_receipt[
            "entry_complete_state_sha256"
        ],
        "calls": call_results,
        "ticks": flat_ticks,
        "final_state": final_snapshot,
        "final_state_sha256": canonical_sha256(final_snapshot),
        "continuation_entry_state": call_results[-1]["continuation_entry_state"],
    }


def sequence_call_as_v1_timeline(
    sequence: Mapping[str, Any], call_index: int
) -> dict[str, Any]:
    """Project one executed sequence call into the existing compositor contract."""

    if sequence.get("schema_version") != SEQUENCE_SCHEMA_VERSION:
        raise DeterminismError("sequence_projection:schema")
    calls = sequence.get("calls")
    inputs = sequence.get("explicit_inputs")
    explicit_calls = inputs.get("calls") if isinstance(inputs, Mapping) else None
    if not isinstance(calls, list) or not isinstance(explicit_calls, list):
        raise DeterminismError("sequence_projection:calls")
    if not _is_plain_integer(call_index) or not 0 <= call_index < len(calls):
        raise DeterminismError(f"sequence_projection:index:{call_index}")
    call = calls[call_index]
    call_input = explicit_calls[call_index]
    if not isinstance(call, Mapping) or not isinstance(call_input, Mapping):
        raise DeterminismError("sequence_projection:call_shape")

    ticks = json.loads(json.dumps(call.get("ticks")))
    if not isinstance(ticks, list) or not ticks:
        raise DeterminismError("sequence_projection:ticks")
    for local_tick, tick in enumerate(ticks):
        if not isinstance(tick, dict) or tick.get("call_tick") != local_tick:
            raise DeterminismError("sequence_projection:tick_order")
        tick["tick"] = local_tick
        tick.pop("call_index", None)
        tick.pop("call_tick", None)
        tick.pop("global_tick", None)

    branch = call_input.get("branch")
    body_name = 'ANMT_INTRO' if branch == 'hunting_intro' else 'ANMT_MOOOON' if branch == 'graveyard_event' else "ANMTSUNDAY" if branch == "sunday" else "ANMT001"
    sequence_scope = sequence.get("parser_scope")
    routine_spans = sequence_scope.get("routine_spans") if isinstance(
        sequence_scope, Mapping
    ) else None
    if not isinstance(routine_spans, Mapping):
        raise DeterminismError("sequence_projection:routine_spans")
    used_names = ["ANIME_INIT", body_name]
    if branch not in ("sunday",'hunting_intro','graveyard_event'):
        used_names.insert(1, "ANIME_WIDW")
    projected_spans = {
        name: routine_spans[name]
        for name in used_names
        if name in routine_spans
    }
    if len(projected_spans) != len(used_names):
        raise DeterminismError("sequence_projection:routine_span_missing")

    explicit_input = {
        "branch": branch,
        "expected_state": call_input.get("expected_state"),
        "tick_count": call_input.get("tick_count"),
        "random_values": call_input.get("random_values"),
        "provenance": "caller_explicit_sequence_call",
        "entry_state": {
            "mode": "complete_call_entry_from_sequence",
            "sequence_call_index": call_index,
            "entry_complete_state_sha256": call.get(
                "entry_complete_state_sha256"
            ),
            "hidden_state_inference": "forbidden",
        },
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "scene": json.loads(json.dumps(sequence.get("scene"))),
        "sources": json.loads(json.dumps(sequence.get("sources"))),
        "parser_scope": {
            "grammar": "restricted_numeric_activity_v1",
            "initialization": "single_ANIME_INIT_shared_runtime_sequence_projection",
            "selection": "single_ANIME_WIDW_or_ANMTSUNDAY",
            "body": "ANMT001_or_ANMTSUNDAY_prefix",
            "routine_spans": projected_spans,
            "source_default_tick_count": call.get("source_default_tick_count"),
            "requested_tick_count": call_input.get("tick_count"),
            "complete_source_loop": True,
            "sequence_call_index": call_index,
        },
        "explicit_inputs": explicit_input,
        "random_receipts": json.loads(json.dumps(call.get("random_receipts"))),
        "initialized_state_sha256": sequence.get("initialized_state_sha256"),
        "initialized_complete_state_sha256": sequence.get(
            "initialized_complete_state_sha256"
        ),
        "entry_complete_state_sha256": call.get("entry_complete_state_sha256"),
        "selection": json.loads(json.dumps(call.get("selection"))),
        "ticks": ticks,
        "final_state": json.loads(json.dumps(call.get("final_state"))),
        "final_state_sha256": call.get("final_state_sha256"),
    }


def verify_source_checkout(source_root: Path, expected_commit: str, scene_id: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{40}", expected_commit):
        raise SourceIdentityError("expected_commit_format")
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SourceIdentityError("git_revision_unavailable") from error
    actual = completed.stdout.strip().lower()
    if actual != expected_commit:
        raise SourceIdentityError(f"commit:{actual}")
    relative_path = f"KOSOTEXT/{scene_id}.TXT"
    try:
        tracked = subprocess.run(
            ["git", "-C", str(source_root), "ls-files", "--error-unmatch", relative_path],
            check=False,
            capture_output=True,
        )
        unchanged = subprocess.run(
            ["git", "-C", str(source_root), "diff", "--quiet", expected_commit, "--", relative_path],
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise SourceIdentityError("git_file_identity_unavailable") from error
    if tracked.returncode != 0 or unchanged.returncode != 0:
        raise SourceIdentityError(f"scene_file:{scene_id}")
    return actual


def parse_random_values(value: str) -> list[int]:
    normalized = value.strip().lower()
    if normalized in {"none", "empty", "[]"}:
        return []
    try:
        return [int(item.strip()) for item in value.split(",") if item.strip()]
    except ValueError as error:
        raise MissingInputError("random_values_format") from error


def read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise MissingInputError(f"{label}_json") from error
    if not isinstance(value, dict):
        raise MissingInputError(f"{label}_object")
    return value


def _write_new_output(path: Path, encoded: str) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise TimelineError("output_parent") from error
    if os.path.lexists(path):
        raise TimelineError("output_already_exists")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="xb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_name = handle.name
            handle.write(encoded.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary_name, path)
    except FileExistsError as error:
        raise TimelineError("output_already_exists") from error
    except OSError as error:
        raise TimelineError("output_write") from error
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Emit a redacted deterministic PM2 JOB003/JOB008 numeric timeline."
    )
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--scene", choices=SUPPORTED_SCENES, required=True)
    parser.add_argument("--branch", choices=SUPPORTED_BRANCHES)
    parser.add_argument("--state", type=int)
    parser.add_argument("--ticks", type=int)
    parser.add_argument(
        "--random-values",
        help="Comma-separated RANDAM results in source execution order, or 'none'.",
    )
    parser.add_argument(
        "--entry-state",
        type=Path,
        help="Optional explicit single-call entry-state JSON object.",
    )
    parser.add_argument(
        "--sequence-spec",
        type=Path,
        help=(
            "JSON object with initialization_random_values, entry_state, and calls; "
            "mutually exclusive with single-call arguments."
        ),
    )
    parser.add_argument(
        "--sequence-call-index",
        type=int,
        help="With --sequence-spec, emit one call as a compositor-compatible v1 timeline.",
    )
    parser.add_argument("--expected-commit", default=FIXED_SOURCE_COMMIT)
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        commit = verify_source_checkout(args.source_root, args.expected_commit, args.scene)
        source_path = args.source_root / "KOSOTEXT" / f"{args.scene}.TXT"
        if args.sequence_spec is not None:
            if any(
                value is not None
                for value in (
                    args.branch,
                    args.state,
                    args.ticks,
                    args.random_values,
                    args.entry_state,
                )
            ):
                raise DeterminismError("sequence_and_single_call_arguments")
            spec = read_json_object(args.sequence_spec, "sequence_spec")
            if set(spec) != {"initialization_random_values", "entry_state", "calls"}:
                raise DeterminismError("sequence_spec_fields")
            timeline = build_timeline_sequence(
                source_path,
                args.scene,
                initialization_random_values=spec.get("initialization_random_values"),
                entry_state=spec.get("entry_state"),
                calls=spec.get("calls"),
                source_commit=commit,
            )
            if args.sequence_call_index is not None:
                timeline = sequence_call_as_v1_timeline(
                    timeline, args.sequence_call_index
                )
        else:
            if args.sequence_call_index is not None:
                raise DeterminismError("sequence_call_index_without_sequence")
            entry_state = (
                None
                if args.entry_state is None
                else read_json_object(args.entry_state, "entry_state")
            )
            timeline = build_timeline(
                source_path,
                args.scene,
                branch=args.branch,
                expected_state=args.state,
                tick_count=args.ticks,
                random_values=(
                    None
                    if args.random_values is None
                    else parse_random_values(args.random_values)
                ),
                source_commit=commit,
                entry_state=entry_state,
            )
    except TimelineError as error:
        print(str(error), file=sys.stderr)
        return 2
    encoded = json.dumps(timeline, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    if args.output is None:
        sys.stdout.write(encoded)
    else:
        try:
            _write_new_output(args.output, encoded)
        except TimelineError as error:
            print(str(error), file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
