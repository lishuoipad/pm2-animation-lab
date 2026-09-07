#!/usr/bin/env python3
"""Create a hash-only PM2 activity capture receipt inside the quarantine.

The caller supplies every identity and sequence contract explicitly.  This
tool verifies the fixed archive, source MKV, FFmpeg framehash, complete PNG
sequence, and named environment component files before atomically creating one
JSON receipt.  It copies no source bytes into the receipt and makes no semantic,
S2, or authorization decision.
"""

from __future__ import annotations
from pm2_animation_lab.paths import is_data_directory

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image


TOOL_VERSION = "0.2.0"
RECEIPT_KIND = "pm2_activity_capture_receipt"
MAX_SOURCE_FRAMES = 100_000
MAX_SOURCE_DIMENSION = 4096
MAX_SOURCE_PIXELS = 16_777_216
RAW_CAPTURE_WIDTH = 640
RAW_CAPTURE_HEIGHT = 480
MAX_ARGV_ITEMS = 512
MAX_ARG_LENGTH = 32_768
MAX_PROVENANCE_TEXT_BYTES = 16 * 1024 * 1024

_SAFE_ID_RE = re.compile(r"[A-Za-z0-9_.-]+")
_FRAME_PATTERN_RE = re.compile(r"([A-Za-z0-9_.-]*)%0([1-9][0-9]?)d\.png")


class CaptureReceiptError(ValueError):
    """Raised when a receipt input or explicit contract is invalid."""


class QuarantineError(CaptureReceiptError):
    """Raised when an input or output escapes the external-data quarantine."""


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _normalize_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise CaptureReceiptError(f"{label} must be a SHA-256 hex string")
    try:
        int(value, 16)
    except ValueError as exc:
        raise CaptureReceiptError(f"{label} must be a SHA-256 hex string") from exc
    return value.lower()


def _require_integer(value: object, label: str, *, minimum: int = 0) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise CaptureReceiptError(f"{label} must be an integer >= {minimum}")
    return value


def _require_path_value(value: object, label: str) -> Path:
    if not isinstance(value, (str, Path)) or not os.fspath(value):
        raise CaptureReceiptError(f"{label} path is required")
    return Path(value)


def _resolve_quarantine_root(root: Path) -> Path:
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise QuarantineError("quarantine root is unavailable") from exc
    if not is_data_directory(resolved):
        raise QuarantineError("quarantine root must be an existing external data directory")
    return resolved


def _require_quarantined_path(
    path: Path,
    root: Path,
    *,
    must_exist: bool,
) -> Path:
    try:
        resolved = path.resolve(strict=must_exist)
    except OSError as exc:
        raise QuarantineError(f"quarantined path is unavailable: {path}") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise QuarantineError(f"path escapes quarantine root: {path}") from exc
    if resolved == root:
        raise QuarantineError("an input or output path cannot equal the quarantine root")
    return resolved


def _require_file(path: Path, root: Path, label: str) -> Path:
    resolved = _require_quarantined_path(path, root, must_exist=True)
    if not resolved.is_file():
        raise CaptureReceiptError(f"{label} must be a file: {resolved}")
    return resolved


def _require_directory(path: Path, root: Path, label: str) -> Path:
    resolved = _require_quarantined_path(path, root, must_exist=True)
    if not resolved.is_dir():
        raise CaptureReceiptError(f"{label} must be a directory: {resolved}")
    return resolved


def _relative_path(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _verified_file_binding(
    path: Path,
    expected_sha256: object,
    root: Path,
    label: str,
) -> tuple[Path, dict[str, Any]]:
    resolved = _require_file(path, root, label)
    expected = _normalize_sha256(expected_sha256, f"{label} expected_sha256")
    actual = sha256_path(resolved)
    if actual != expected:
        raise CaptureReceiptError(
            f"{label} SHA-256 mismatch: expected {expected}, got {actual}"
        )
    return resolved, {
        "path": _relative_path(resolved, root),
        "sha256": actual,
        "size_bytes": resolved.stat().st_size,
    }


def _read_provenance_text(path: Path, label: str) -> str:
    size = path.stat().st_size
    if size == 0:
        raise CaptureReceiptError(f"{label} must not be empty")
    if size > MAX_PROVENANCE_TEXT_BYTES:
        raise CaptureReceiptError(f"{label} exceeds the provenance text size limit")
    raw = path.read_bytes()
    for encoding in ("utf-8-sig", "utf-16"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise CaptureReceiptError(f"{label} must be UTF-8 or UTF-16 text")
    if "\x00" in text:
        raise CaptureReceiptError(f"{label} contains NUL characters")
    return text


def _verified_provenance_text_binding(
    path: object,
    expected_sha256: object,
    root: Path,
    label: str,
    *,
    require_ffmpeg_version: bool = False,
) -> tuple[Path, dict[str, Any]]:
    resolved, binding = _verified_file_binding(
        _require_path_value(path, label), expected_sha256, root, label
    )
    text = _read_provenance_text(resolved, label)
    if require_ffmpeg_version:
        first_nonempty = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if not first_nonempty.casefold().startswith("ffmpeg version "):
            raise CaptureReceiptError(
                "FFmpeg version text must begin with an 'ffmpeg version' line"
            )
    return resolved, binding


def _require_unique_flag(argv: Sequence[str], flag: str, label: str) -> None:
    count = sum(argument == flag for argument in argv)
    if count != 1:
        raise CaptureReceiptError(f"{label} argv must contain {flag} exactly once")


def _require_unique_option_value(
    argv: Sequence[str], option: str, expected: str, label: str
) -> None:
    indexes = [index for index, argument in enumerate(argv) if argument == option]
    if len(indexes) != 1:
        raise CaptureReceiptError(f"{label} argv must contain {option} exactly once")
    index = indexes[0]
    if index + 1 >= len(argv) or argv[index + 1].casefold() != expected.casefold():
        raise CaptureReceiptError(f"{label} argv must use {option} {expected}")


def _resolve_absolute_argv_path(
    value: str,
    root: Path,
    label: str,
    *,
    must_exist: bool,
) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        raise CaptureReceiptError(f"{label} must be an absolute path")
    return _require_quarantined_path(candidate, root, must_exist=must_exist)


def _validate_ffmpeg_argv(
    value: object,
    root: Path,
    *,
    label: str,
    ffmpeg_executable: Path,
    source_mkv: Path,
    expected_output: Path,
    output_must_exist: bool,
    command_kind: str,
    first_frame: int,
) -> list[str]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence) or not value:
        raise CaptureReceiptError(f"{label} argv must be a non-empty array of strings")
    if len(value) > MAX_ARGV_ITEMS:
        raise CaptureReceiptError(f"{label} argv contains too many items")

    argv: list[str] = []
    for index, argument in enumerate(value):
        if not isinstance(argument, str) or not argument:
            raise CaptureReceiptError(f"{label} argv[{index}] must be a non-empty string")
        if len(argument) > MAX_ARG_LENGTH:
            raise CaptureReceiptError(f"{label} argv[{index}] exceeds the length limit")
        if any(character in argument for character in ("\x00", "\r", "\n")):
            raise CaptureReceiptError(f"{label} argv[{index}] contains a forbidden character")
        if argument.startswith("@"):
            raise CaptureReceiptError(
                f"{label} argv must not use response files; the complete argv is required"
            )
        argv.append(argument)

    executable_arg = _resolve_absolute_argv_path(
        argv[0], root, f"{label} argv[0]", must_exist=True
    )
    if executable_arg != ffmpeg_executable:
        raise CaptureReceiptError(f"{label} argv[0] does not match the bound FFmpeg executable")

    input_indexes = [index for index, argument in enumerate(argv) if argument == "-i"]
    if len(input_indexes) != 1 or input_indexes[0] + 1 >= len(argv):
        raise CaptureReceiptError(f"{label} argv must contain exactly one -i input")
    source_arg = _resolve_absolute_argv_path(
        argv[input_indexes[0] + 1], root, f"{label} input", must_exist=True
    )
    if source_arg != source_mkv:
        raise CaptureReceiptError(f"{label} input does not match the bound source MKV")

    output_arg = _resolve_absolute_argv_path(
        argv[-1], root, f"{label} output", must_exist=output_must_exist
    )
    if output_arg != expected_output:
        raise CaptureReceiptError(f"{label} output does not match its bound artifact")

    for argument in argv[1:]:
        candidate = Path(argument)
        if candidate.is_absolute():
            _require_quarantined_path(candidate, root, must_exist=False)

    _require_unique_flag(argv, "-nostdin", label)
    _require_unique_flag(argv, "-n", label)
    if "-y" in argv:
        raise CaptureReceiptError(f"{label} argv must not enable overwrite with -y")
    _require_unique_option_value(argv, "-map", "0:v:0", label)
    _require_unique_option_value(argv, "-fps_mode", "passthrough", label)
    _require_unique_option_value(argv, "-pix_fmt", "rgb24", label)

    forbidden_filter_options = {"-vf", "-filter:v", "-filter:v:0", "-filter_complex"}
    if any(argument.casefold() in forbidden_filter_options for argument in argv):
        raise CaptureReceiptError(
            f"{label} argv must not use video filters; crop/scale/fps belong outside raw capture"
        )
    if any(
        argument.casefold() == "-r" or argument.casefold().startswith("-r:")
        for argument in argv
    ):
        raise CaptureReceiptError(f"{label} argv must not set an output frame rate with -r")

    if command_kind == "framehash":
        _require_unique_option_value(argv, "-f", "framehash", label)
        _require_unique_option_value(argv, "-hash", "sha256", label)
    elif command_kind == "png":
        _require_unique_option_value(argv, "-start_number", str(first_frame), label)
    else:  # pragma: no cover - internal invariant
        raise CaptureReceiptError(f"unknown FFmpeg command kind: {command_kind}")
    return argv


def _argv_sha256(argv: Sequence[str]) -> str:
    canonical = json.dumps(
        list(argv), ensure_ascii=True, separators=(",", ":")
    ).encode("ascii")
    return hashlib.sha256(canonical).hexdigest()


def _decode_argv_json(value: object, label: str) -> object:
    if not isinstance(value, str) or not value:
        raise CaptureReceiptError(f"{label} argv JSON is required")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise CaptureReceiptError(f"{label} argv JSON must encode an array") from exc
    if not isinstance(decoded, list):
        raise CaptureReceiptError(f"{label} argv JSON must encode an array")
    return decoded


def _parse_filename_pattern(value: object) -> tuple[str, int, str]:
    if not isinstance(value, str):
        raise CaptureReceiptError("filename_pattern must be a string")
    match = _FRAME_PATTERN_RE.fullmatch(value)
    if match is None:
        raise CaptureReceiptError("filename_pattern must look like frame_%06d.png")
    return match.group(1), int(match.group(2)), value


def _validate_png_sequence(
    png_directory: Path,
    root: Path,
    *,
    filename_pattern: str,
    first_frame: int,
    frame_count: int,
    width: int,
    height: int,
    mode: str,
) -> tuple[Path, dict[str, Any]]:
    directory = _require_directory(png_directory, root, "PNG sequence directory")
    if mode != "RGB":
        raise CaptureReceiptError("PNG sequence mode must be RGB")
    if (
        width > MAX_SOURCE_DIMENSION
        or height > MAX_SOURCE_DIMENSION
        or width * height > MAX_SOURCE_PIXELS
    ):
        raise CaptureReceiptError("PNG sequence dimensions exceed safety limits")
    if frame_count > MAX_SOURCE_FRAMES:
        raise CaptureReceiptError("PNG sequence frame count exceeds safety limit")

    prefix, digits, normalized_pattern = _parse_filename_pattern(filename_pattern)
    last_frame = first_frame + frame_count - 1
    if len(str(last_frame)) > digits:
        raise CaptureReceiptError("PNG frame numbers exceed filename pattern width")
    expected_names = [
        f"{prefix}{frame_number:0{digits}d}.png"
        for frame_number in range(first_frame, last_frame + 1)
    ]
    actual_names = sorted(
        entry.name for entry in directory.iterdir() if entry.is_file() and entry.suffix.lower() == ".png"
    )
    if actual_names != sorted(expected_names):
        missing = sorted(set(expected_names) - set(actual_names))
        unexpected = sorted(set(actual_names) - set(expected_names))
        raise CaptureReceiptError(
            "PNG sequence is not complete and continuous: "
            f"missing={missing[:5]}, unexpected={unexpected[:5]}"
        )

    for name in expected_names:
        frame_path = _require_file(directory / name, root, "PNG sequence frame")
        try:
            with Image.open(frame_path) as image:
                if image.format != "PNG":
                    raise CaptureReceiptError(f"sequence frame is not a PNG: {frame_path}")
                if getattr(image, "n_frames", 1) != 1:
                    raise CaptureReceiptError(
                        f"sequence frame must be one non-animated PNG: {frame_path}"
                    )
                if image.mode != mode:
                    raise CaptureReceiptError(
                        f"sequence frame mode mismatch for {frame_path}: expected {mode}, got {image.mode}"
                    )
                if image.size != (width, height):
                    raise CaptureReceiptError(
                        f"sequence frame dimensions mismatch for {frame_path}: "
                        f"expected {width}x{height}, got {image.width}x{image.height}"
                    )
        except CaptureReceiptError:
            raise
        except (OSError, ValueError) as exc:
            raise CaptureReceiptError(f"cannot inspect PNG sequence frame: {frame_path}") from exc

    return directory, {
        "directory": _relative_path(directory, root),
        "filename_pattern": normalized_pattern,
        "first_frame": first_frame,
        "frame_count": frame_count,
        "width": width,
        "height": height,
        "mode": mode,
    }


def _validate_environment_components(
    components: Sequence[Mapping[str, object]],
    root: Path,
) -> list[dict[str, Any]]:
    if isinstance(components, (str, bytes)) or not components:
        raise CaptureReceiptError("environment_components must be a non-empty list")
    validated: list[dict[str, Any]] = []
    seen_roles: set[str] = set()
    for index, component in enumerate(components):
        if not isinstance(component, Mapping):
            raise CaptureReceiptError(f"environment_components[{index}] must be an object")
        role = component.get("role")
        if not isinstance(role, str) or _SAFE_ID_RE.fullmatch(role) is None:
            raise CaptureReceiptError(
                f"environment_components[{index}].role must be a non-empty safe identifier"
            )
        folded = role.casefold()
        if folded in seen_roles:
            raise CaptureReceiptError(f"environment component role is not unique: {role}")
        seen_roles.add(folded)
        raw_path = component.get("path")
        if not isinstance(raw_path, (str, Path)):
            raise CaptureReceiptError(f"environment_components[{index}].path is required")
        resolved, binding = _verified_file_binding(
            Path(raw_path),
            component.get("sha256"),
            root,
            f"environment component {role}",
        )
        validated.append({"role": role, **binding})
        # Keep the resolved path live through validation; this also documents
        # that directory/symlink escapes were rejected before serialization.
        del resolved
    return sorted(validated, key=lambda item: item["role"].casefold())


def create_capture_receipt(
    archive_path: Path,
    source_mkv_path: Path,
    framehash_path: Path,
    png_directory: Path,
    output_path: Path,
    quarantine_root: Path,
    *,
    scene_id: str,
    archive_sha256: str,
    source_mkv_sha256: str,
    framehash_sha256: str,
    filename_pattern: str,
    first_frame: int,
    frame_count: int,
    width: int,
    height: int,
    mode: str,
    stream_index: int,
    framehash_hash_algorithm: str,
    framehash_pixel_format: str,
    environment_components: Sequence[Mapping[str, object]],
    ffmpeg_executable_path: Path | None,
    ffmpeg_executable_sha256: str | None,
    ffmpeg_version_path: Path | None,
    ffmpeg_version_sha256: str | None,
    framehash_argv: Sequence[str] | None,
    framehash_command_log_path: Path | None,
    framehash_command_log_sha256: str | None,
    png_argv: Sequence[str] | None,
    png_command_log_path: Path | None,
    png_command_log_sha256: str | None,
) -> dict[str, Any]:
    """Validate all identities, then atomically create one safe receipt JSON."""

    root = _resolve_quarantine_root(quarantine_root)
    output = _require_quarantined_path(output_path, root, must_exist=False)
    if output.exists():
        raise QuarantineError("capture receipt output already exists")
    if output.suffix.lower() != ".json":
        raise CaptureReceiptError("capture receipt output must use the .json extension")
    if not output.parent.is_dir():
        raise QuarantineError("capture receipt output parent must already exist")
    if not isinstance(scene_id, str) or _SAFE_ID_RE.fullmatch(scene_id) is None:
        raise CaptureReceiptError("scene_id must be a non-empty safe identifier")

    first = _require_integer(first_frame, "first_frame")
    count = _require_integer(frame_count, "frame_count", minimum=1)
    sequence_width = _require_integer(width, "width", minimum=1)
    sequence_height = _require_integer(height, "height", minimum=1)
    selected_stream = _require_integer(stream_index, "stream_index")
    if (sequence_width, sequence_height) != (RAW_CAPTURE_WIDTH, RAW_CAPTURE_HEIGHT):
        raise CaptureReceiptError(
            f"raw capture PNG/framehash dimensions must be "
            f"{RAW_CAPTURE_WIDTH}x{RAW_CAPTURE_HEIGHT}; crop only in runtime capture"
        )
    if selected_stream != 0:
        raise CaptureReceiptError("stream_index must be 0 for the required 0:v:0 capture map")
    if (
        not isinstance(framehash_hash_algorithm, str)
        or framehash_hash_algorithm.lower() != "sha256"
    ):
        raise CaptureReceiptError("framehash_hash_algorithm must be sha256")
    if (
        not isinstance(framehash_pixel_format, str)
        or framehash_pixel_format.lower() != "rgb24"
    ):
        raise CaptureReceiptError("framehash_pixel_format must be rgb24")

    archive, archive_binding = _verified_file_binding(
        archive_path, archive_sha256, root, "fixed archive"
    )
    source_mkv, mkv_binding = _verified_file_binding(
        source_mkv_path, source_mkv_sha256, root, "source MKV"
    )
    if source_mkv.suffix.lower() != ".mkv":
        raise CaptureReceiptError("source MKV must use the .mkv extension")
    framehash, framehash_binding = _verified_file_binding(
        framehash_path, framehash_sha256, root, "FFmpeg framehash"
    )
    png_root, sequence_binding = _validate_png_sequence(
        png_directory,
        root,
        filename_pattern=filename_pattern,
        first_frame=first,
        frame_count=count,
        width=sequence_width,
        height=sequence_height,
        mode=mode,
    )
    environment = _validate_environment_components(environment_components, root)

    ffmpeg_executable, ffmpeg_executable_binding = _verified_file_binding(
        _require_path_value(ffmpeg_executable_path, "FFmpeg executable"),
        ffmpeg_executable_sha256,
        root,
        "FFmpeg executable",
    )
    if ffmpeg_executable.name.casefold() not in {"ffmpeg", "ffmpeg.exe"}:
        raise CaptureReceiptError("FFmpeg executable must be the absolute ffmpeg or ffmpeg.exe file")
    ffmpeg_version, ffmpeg_version_binding = _verified_provenance_text_binding(
        ffmpeg_version_path,
        ffmpeg_version_sha256,
        root,
        "FFmpeg version text",
        require_ffmpeg_version=True,
    )
    framehash_command_log, framehash_log_binding = _verified_provenance_text_binding(
        framehash_command_log_path,
        framehash_command_log_sha256,
        root,
        "framehash command execution log",
    )
    png_command_log, png_log_binding = _verified_provenance_text_binding(
        png_command_log_path,
        png_command_log_sha256,
        root,
        "PNG command execution log",
    )
    if framehash_command_log == png_command_log:
        raise CaptureReceiptError(
            "framehash and PNG commands must have separate execution log files"
        )

    png_output_pattern = _require_quarantined_path(
        png_root / sequence_binding["filename_pattern"], root, must_exist=False
    )
    validated_framehash_argv = _validate_ffmpeg_argv(
        framehash_argv,
        root,
        label="framehash command",
        ffmpeg_executable=ffmpeg_executable,
        source_mkv=source_mkv,
        expected_output=framehash,
        output_must_exist=True,
        command_kind="framehash",
        first_frame=first,
    )
    validated_png_argv = _validate_ffmpeg_argv(
        png_argv,
        root,
        label="PNG extraction command",
        ffmpeg_executable=ffmpeg_executable,
        source_mkv=source_mkv,
        expected_output=png_output_pattern,
        output_must_exist=False,
        command_kind="png",
        first_frame=first,
    )

    # Recheck all caller-provided hash bindings immediately before
    # serialization so a long PNG inspection cannot silently stale them.
    for path, binding, label in (
        (archive, archive_binding, "fixed archive"),
        (source_mkv, mkv_binding, "source MKV"),
        (framehash, framehash_binding, "FFmpeg framehash"),
        (ffmpeg_executable, ffmpeg_executable_binding, "FFmpeg executable"),
        (ffmpeg_version, ffmpeg_version_binding, "FFmpeg version text"),
        (framehash_command_log, framehash_log_binding, "framehash command execution log"),
        (png_command_log, png_log_binding, "PNG command execution log"),
    ):
        current = sha256_path(path)
        if current != binding["sha256"]:
            raise CaptureReceiptError(f"{label} changed during receipt validation")
    for component in environment:
        component_path = _require_file(root / component["path"], root, component["role"])
        if sha256_path(component_path) != component["sha256"]:
            raise CaptureReceiptError(
                f"environment component changed during receipt validation: {component['role']}"
            )
    # Resolve the sequence directory once more to ensure it has not been
    # replaced by an escape before the bound relative path is emitted.
    _require_directory(png_root, root, "PNG sequence directory")

    receipt = {
        "schema_version": 1,
        "receipt_kind": RECEIPT_KIND,
        "status": "ready",
        "tool_version": TOOL_VERSION,
        "scene_id": scene_id,
        # Flat hashes are consumed by the runtime-crop bridge.  The nested
        # bindings retain the corresponding quarantine-relative identities.
        "archive_sha256": archive_binding["sha256"],
        "source_mkv_sha256": mkv_binding["sha256"],
        "framehash_sha256": framehash_binding["sha256"],
        "archive": archive_binding,
        "source_mkv": mkv_binding,
        "framehash": {
            **framehash_binding,
            "stream_index": selected_stream,
            "hash_algorithm": "sha256",
            "pixel_format": "rgb24",
        },
        "png_sequence": sequence_binding,
        "ffmpeg_provenance": {
            "executable": ffmpeg_executable_binding,
            "version_text": ffmpeg_version_binding,
            "commands": {
                "framehash": {
                    "argv": validated_framehash_argv,
                    "argv_sha256": _argv_sha256(validated_framehash_argv),
                    "execution_log": framehash_log_binding,
                },
                "png_extraction": {
                    "argv": validated_png_argv,
                    "argv_sha256": _argv_sha256(validated_png_argv),
                    "execution_log": png_log_binding,
                },
            },
        },
        "environment_components": environment,
        "semantic_interpretation": None,
        "automatic_s2_promotion": False,
        "authorization_effect": "none",
        "safety": {
            "contains_original_payload": False,
            "contains_original_pixels": False,
            "contains_original_text": False,
            "contains_palette_values": False,
            "game_build_input": False,
        },
    }
    raw = (json.dumps(receipt, ensure_ascii=True, indent=2, sort_keys=True) + "\n").encode(
        "ascii"
    )

    try:
        with tempfile.TemporaryDirectory(prefix=".pm2_capture_receipt_", dir=output.parent) as temporary:
            staging = Path(temporary) / output.name
            with staging.open("xb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            # A same-volume hard link publishes the fully written file in one
            # step and fails if the destination appeared after the precheck.
            # This preserves the no-overwrite contract on every supported OS.
            os.link(staging, output)
            staging.unlink()
    except OSError as exc:
        raise QuarantineError("failed to atomically write capture receipt") from exc

    return {
        "schema_version": 1,
        "receipt_kind": "pm2_activity_capture_receipt_creation",
        "tool_version": TOOL_VERSION,
        "status": "ready_written",
        "scene_id": scene_id,
        "output_path": _relative_path(output, root),
        "output_sha256": hashlib.sha256(raw).hexdigest(),
        "environment_component_count": len(environment),
        "output_written": True,
        "semantic_interpretation": None,
        "automatic_s2_promotion": False,
        "authorization_effect": "none",
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create a quarantine-only, hash-bound PM2 activity capture receipt without "
            "semantic or S2 promotion."
        )
    )
    parser.add_argument("--quarantine-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--scene", required=True)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--archive-sha256", required=True)
    parser.add_argument("--source-mkv", type=Path, required=True)
    parser.add_argument("--source-mkv-sha256", required=True)
    parser.add_argument("--framehash", type=Path, required=True)
    parser.add_argument("--framehash-sha256", required=True)
    parser.add_argument("--png-directory", type=Path, required=True)
    parser.add_argument("--filename-pattern", required=True)
    parser.add_argument("--first-frame", type=int, required=True)
    parser.add_argument("--frame-count", type=int, required=True)
    parser.add_argument("--width", type=int, required=True)
    parser.add_argument("--height", type=int, required=True)
    parser.add_argument("--mode", choices=("RGB",), required=True)
    parser.add_argument("--stream-index", type=int, required=True)
    parser.add_argument("--framehash-hash-algorithm", choices=("sha256",), required=True)
    parser.add_argument("--framehash-pixel-format", choices=("rgb24",), required=True)
    # These provenance fields are validated as mandatory by
    # create_capture_receipt.  They remain argparse-optional so every missing
    # provenance edge exits through the same concise domain-error path.
    parser.add_argument("--ffmpeg-executable", type=Path)
    parser.add_argument("--ffmpeg-executable-sha256")
    parser.add_argument("--ffmpeg-version-text", type=Path)
    parser.add_argument("--ffmpeg-version-text-sha256")
    parser.add_argument(
        "--framehash-argv-json",
        help="JSON array containing the complete direct FFmpeg framehash argv",
    )
    parser.add_argument("--framehash-command-log", type=Path)
    parser.add_argument("--framehash-command-log-sha256")
    parser.add_argument(
        "--png-argv-json",
        help="JSON array containing the complete direct FFmpeg PNG-extraction argv",
    )
    parser.add_argument("--png-command-log", type=Path)
    parser.add_argument("--png-command-log-sha256")
    parser.add_argument(
        "--environment-component",
        action="append",
        nargs=3,
        metavar=("ROLE", "PATH", "SHA256"),
        required=True,
        help="repeat for each environment component",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    components = [
        {"role": role, "path": Path(path), "sha256": sha256}
        for role, path, sha256 in args.environment_component
    ]
    try:
        framehash_argv = _decode_argv_json(
            args.framehash_argv_json, "framehash command"
        )
        png_argv = _decode_argv_json(args.png_argv_json, "PNG extraction command")
        result = create_capture_receipt(
            args.archive,
            args.source_mkv,
            args.framehash,
            args.png_directory,
            args.output,
            args.quarantine_root,
            scene_id=args.scene,
            archive_sha256=args.archive_sha256,
            source_mkv_sha256=args.source_mkv_sha256,
            framehash_sha256=args.framehash_sha256,
            filename_pattern=args.filename_pattern,
            first_frame=args.first_frame,
            frame_count=args.frame_count,
            width=args.width,
            height=args.height,
            mode=args.mode,
            stream_index=args.stream_index,
            framehash_hash_algorithm=args.framehash_hash_algorithm,
            framehash_pixel_format=args.framehash_pixel_format,
            environment_components=components,
            ffmpeg_executable_path=args.ffmpeg_executable,
            ffmpeg_executable_sha256=args.ffmpeg_executable_sha256,
            ffmpeg_version_path=args.ffmpeg_version_text,
            ffmpeg_version_sha256=args.ffmpeg_version_text_sha256,
            framehash_argv=framehash_argv,
            framehash_command_log_path=args.framehash_command_log,
            framehash_command_log_sha256=args.framehash_command_log_sha256,
            png_argv=png_argv,
            png_command_log_path=args.png_command_log,
            png_command_log_sha256=args.png_command_log_sha256,
        )
    except CaptureReceiptError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
