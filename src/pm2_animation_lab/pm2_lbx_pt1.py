"""Strict, read-only helpers for studying PM2 DOS LBX/PT1 data.

This module intentionally contains no PM2 payloads, palettes, images, or text.
Callers must keep decoded original material in the project's external external-data
research quarantine.  The routines here are deterministic format primitives;
they do not claim that a reconstructed frame matches a running DOS build.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence


TOOL_VERSION = "0.1.0"
FIXED_SOURCE_COMMIT = "ec7bdef58357185fe5344973c156b857a5de2c1f"
FIXED_ARCHIVE_SHA256 = "d05e57cc8204a68263e4c93e09dc24247820181d176ed4f7f336368cc78ebbdd"
DEFAULT_MAX_LBX_ENTRIES = 4096
DEFAULT_MAX_ZIP_MEMBER_BYTES = 128 * 1024 * 1024
DEFAULT_MAX_PT1_RECORDS = 8192
DEFAULT_MAX_RECORD_PAYLOAD_BYTES = 16 * 1024 * 1024
DEFAULT_MAX_DECOMPRESSED_RECORD_BYTES = 64 * 1024 * 1024
DEFAULT_MAX_TOTAL_DECOMPRESSED_BYTES = 256 * 1024 * 1024


class FormatError(ValueError):
    """Raised when untrusted LBX/PT1 input violates the documented format."""


def _atomic_write_text(path: Path, serialized: str) -> None:
    """Publish a new UTF-8 report without ever replacing existing evidence."""

    if not path.parent.is_dir():
        raise FormatError(f"output parent does not exist: {path.parent}")
    if os.path.lexists(path):
        raise FormatError("output already exists")
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
            handle.write(serialized.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        # A same-volume hard link publishes atomically and fails if a racing
        # writer created the destination after the precheck.
        os.link(temporary_name, path)
    except FileExistsError as exc:
        raise FormatError("output already exists") from exc
    except OSError as exc:
        raise FormatError("failed to atomically write PT1 report") from exc
    finally:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass


@dataclass(frozen=True)
class LbxEntry:
    name: str
    offset: int
    length: int


@dataclass(frozen=True)
class Pt1Record:
    index: int
    record_offset: int
    attribute: int
    author_x_raw: int
    author_y_raw: int
    width_bytes: int
    height_pixels: int
    payload_length: int
    payload: bytes

    @property
    def author_x_pixels(self) -> int:
        """Author X offset converted from eight-pixel byte columns."""

        return self.author_x_raw * 8

    @property
    def author_y_pixels(self) -> int:
        return self.author_y_raw

    @property
    def width_pixels(self) -> int:
        return self.width_bytes * 8

    @property
    def plane_size(self) -> int:
        return self.width_bytes * self.height_pixels


@dataclass(frozen=True)
class Pt1ParseResult:
    records: tuple[Pt1Record, ...]
    terminator: str
    bytes_consumed: int

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)

    def __getitem__(self, index):
        return self.records[index]


@dataclass(frozen=True)
class DecompressionStats:
    input_bytes: int
    output_bytes: int
    block_count: int
    literal_blocks: int
    copy_blocks: int
    literal_words: int
    copy_words: int
    maximum_copy_distance: int
    bytes_consumed: int
    trailing_bytes: int
    terminator_found: bool


@dataclass(frozen=True)
class DecodedPattern:
    """One exact mask/body pair before placement on a destination surface.

    ``mask_bits`` and ``color_indices`` contain one byte per pixel.  A mask bit
    of one preserves the destination planes; zero clears them before the body
    is ORed in.  Normal activity sprites use body index zero where mask is one,
    which also permits ``opaque_bits`` to be used as conventional alpha.
    """

    width_bytes: int
    height_pixels: int
    author_x_raw: int
    author_y_raw: int
    mask_bits: bytes
    color_indices: bytes

    @property
    def width_pixels(self) -> int:
        return self.width_bytes * 8

    @property
    def author_x_pixels(self) -> int:
        return self.author_x_raw * 8

    @property
    def author_y_pixels(self) -> int:
        return self.author_y_raw

    @property
    def opaque_bits(self) -> bytes:
        return bytes(1 - value for value in self.mask_bits)


@dataclass(frozen=True)
class PaletteParameters:
    """Decoded 96-byte PM2 palette parameter record, not an RGB palette."""

    low_hue: tuple[int, ...]
    low_saturation: tuple[int, ...]
    low_brightness: tuple[int, ...]
    high_hue: tuple[int, ...]
    high_saturation: tuple[int, ...]
    high_brightness: tuple[int, ...]

    def hardware_slots(self) -> tuple[tuple[int, int, int], ...]:
        """Return HSV-like parameters in hardware color-index order 0..15."""

        slots: list[tuple[int, int, int] | None] = [None] * 16
        for source, destination in enumerate((0, 9, 10, 11, 12, 13, 14, 15)):
            slots[destination] = (
                self.low_hue[source],
                self.low_saturation[source],
                self.low_brightness[source],
            )
        for source, destination in enumerate((8, 1, 2, 3, 4, 5, 6, 7)):
            slots[destination] = (
                self.high_hue[source],
                self.high_saturation[source],
                self.high_brightness[source],
            )
        if any(value is None for value in slots):  # defensive invariant
            raise AssertionError("palette hardware-slot mapping is incomplete")
        return tuple(value for value in slots if value is not None)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_lbx_directory(
    data: bytes,
    *,
    max_entries: int = DEFAULT_MAX_LBX_ENTRIES,
) -> list[LbxEntry]:
    """Parse an LBX tail directory and reject ambiguous or overlapping data."""

    if max_entries <= 0:
        raise ValueError("max_entries must be positive")
    if len(data) < 6:
        raise FormatError("LBX data is too short for the six-byte footer")

    count, directory_offset = struct.unpack_from("<HI", data, len(data) - 6)
    if count == 0:
        raise FormatError("LBX directory contains no entries")
    if count > max_entries:
        raise FormatError(f"LBX entry count {count} exceeds limit {max_entries}")

    footer_offset = len(data) - 6
    directory_size = count * 20
    if directory_offset > footer_offset or directory_size > footer_offset - directory_offset:
        raise FormatError(
            f"LBX directory is out of bounds: offset={directory_offset}, count={count}, size={len(data)}"
        )
    if directory_offset + directory_size != footer_offset:
        raise FormatError(
            f"LBX directory must end immediately before the footer: offset={directory_offset}, "
            f"count={count}, size={len(data)}"
        )

    entries: list[LbxEntry] = []
    seen_names: set[str] = set()
    intervals: list[tuple[int, int, str]] = []
    for index in range(count):
        position = directory_offset + index * 20
        raw_name = data[position : position + 12]
        stripped_name = raw_name.rstrip(b" ")
        if not stripped_name:
            raise FormatError(f"LBX entry {index} has an empty name")
        if raw_name[len(stripped_name) :] != b" " * (12 - len(stripped_name)):
            raise FormatError(f"LBX entry {index} name is not space padded")
        if any(value < 0x21 or value > 0x7E for value in stripped_name):
            raise FormatError(f"LBX entry {index} name contains non-printable ASCII")
        try:
            name = stripped_name.decode("ascii")
        except UnicodeDecodeError as exc:  # covered by range check; kept for clarity
            raise FormatError(f"LBX entry {index} name is not ASCII") from exc

        normalized_name = name.upper()
        if normalized_name in seen_names:
            raise FormatError(f"LBX contains duplicate entry name {name!r}")
        seen_names.add(normalized_name)

        offset, length = struct.unpack_from("<II", data, position + 12)
        if offset > directory_offset or length > directory_offset - offset:
            raise FormatError(f"LBX entry {name!r} overlaps the directory or file boundary")
        entries.append(LbxEntry(name=name, offset=offset, length=length))
        if length:
            intervals.append((offset, offset + length, name))

    intervals.sort()
    for previous, current in zip(intervals, intervals[1:]):
        if current[0] < previous[1]:
            raise FormatError(
                f"LBX payloads overlap: {previous[2]!r} [{previous[0]}, {previous[1]}) and "
                f"{current[2]!r} [{current[0]}, {current[1]})"
            )
    return entries


def _zip_basename(name: str) -> str:
    return PurePosixPath(name.replace("\\", "/")).name


def _find_unique_zip_member(archive: zipfile.ZipFile, requested_name: str) -> zipfile.ZipInfo:
    requested = requested_name.replace("\\", "/")
    exact = [
        info
        for info in archive.infolist()
        if info.filename.replace("\\", "/").upper() == requested.upper()
    ]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise FormatError(f"ZIP member {requested_name!r} is duplicated")

    basename = PurePosixPath(requested).name.upper()
    matches = [info for info in archive.infolist() if _zip_basename(info.filename).upper() == basename]
    if not matches:
        raise FormatError(f"ZIP member {requested_name!r} was not found")
    if len(matches) != 1:
        raise FormatError(
            f"ZIP member {requested_name!r} is ambiguous: {[info.filename for info in matches]}"
        )
    return matches[0]


def _read_bounded_zip_member(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    *,
    max_bytes: int,
) -> bytes:
    if max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    if info.is_dir():
        raise FormatError(f"ZIP member {info.filename!r} is a directory")
    if info.flag_bits & 0x1:
        raise FormatError(f"ZIP member {info.filename!r} is encrypted")
    if info.file_size > max_bytes:
        raise FormatError(
            f"ZIP member {info.filename!r} size {info.file_size} exceeds limit {max_bytes}"
        )
    with archive.open(info, "r") as handle:
        data = handle.read(max_bytes + 1)
        if len(data) > max_bytes:
            raise FormatError(f"ZIP member {info.filename!r} expanded beyond limit {max_bytes}")
        if handle.read(1):
            raise FormatError(f"ZIP member {info.filename!r} expanded beyond its bounded read")
    if len(data) != info.file_size:
        raise FormatError(
            f"ZIP member {info.filename!r} size mismatch: metadata={info.file_size}, read={len(data)}"
        )
    return data


def read_lbx_asset_from_zip(
    archive_path: str | Path,
    library_member: str,
    asset_name: str,
    *,
    expected_archive_sha256: str | None = None,
    max_zip_member_bytes: int = DEFAULT_MAX_ZIP_MEMBER_BYTES,
    max_lbx_entries: int = DEFAULT_MAX_LBX_ENTRIES,
) -> tuple[bytes, dict[str, int | str]]:
    """Read one LBX asset without extracting or modifying the source archive."""

    path = Path(archive_path)
    archive_sha256 = sha256_path(path)
    if expected_archive_sha256 is not None and archive_sha256.lower() != expected_archive_sha256.lower():
        raise FormatError(
            f"archive SHA-256 mismatch: expected {expected_archive_sha256.lower()}, got {archive_sha256}"
        )

    with zipfile.ZipFile(path, "r") as archive:
        library_info = _find_unique_zip_member(archive, library_member)
        library_data = _read_bounded_zip_member(
            archive, library_info, max_bytes=max_zip_member_bytes
        )

    entries = parse_lbx_directory(library_data, max_entries=max_lbx_entries)
    matches = [entry for entry in entries if entry.name.upper() == asset_name.upper()]
    if not matches:
        raise FormatError(f"LBX asset {asset_name!r} was not found in {library_info.filename!r}")
    if len(matches) != 1:  # duplicate names are already rejected, retained defensively
        raise FormatError(f"LBX asset {asset_name!r} is ambiguous")
    entry = matches[0]
    payload = library_data[entry.offset : entry.offset + entry.length]

    if sha256_path(path) != archive_sha256:
        raise RuntimeError("source archive changed while it was being read")
    metadata: dict[str, int | str] = {
        "tool_version": TOOL_VERSION,
        "archive_path": str(path.resolve()),
        "archive_sha256": archive_sha256,
        "library_member": library_info.filename,
        "library_length": len(library_data),
        "library_sha256": sha256_bytes(library_data),
        "entry_name": entry.name,
        "entry_offset": entry.offset,
        "entry_length": entry.length,
        "entry_sha256": sha256_bytes(payload),
    }
    return payload, metadata


def decompress_ple(
    data: bytes,
    *,
    max_output_bytes: int = DEFAULT_MAX_DECOMPRESSED_RECORD_BYTES,
    require_complete_input: bool = True,
) -> tuple[bytes, DecompressionStats]:
    """Decode the word-oriented PLE stream used by compressed PT1 records."""

    if max_output_bytes < 0:
        raise ValueError("max_output_bytes must not be negative")
    cursor = 0
    output = bytearray()
    block_count = literal_blocks = copy_blocks = 0
    literal_words = copy_words = maximum_copy_distance = 0
    terminator_found = False

    def read_word(label: str) -> int:
        nonlocal cursor
        if cursor + 2 > len(data):
            raise FormatError(f"truncated PLE stream while reading {label} at byte {cursor}")
        value = struct.unpack_from("<H", data, cursor)[0]
        cursor += 2
        return value

    while True:
        code = read_word("block control")
        if code == 0:
            terminator_found = True
            break
        block_count += 1
        word_count = code & 0x7FFF
        if word_count == 0:
            raise FormatError(f"PLE block {block_count} has a zero word count")
        byte_count = word_count * 2
        if byte_count > max_output_bytes - len(output):
            raise FormatError(
                f"PLE output would exceed limit {max_output_bytes} at block {block_count}"
            )

        if code & 0x8000:
            distance = read_word("copy distance")
            if distance == 0:
                raise FormatError(f"PLE copy block {block_count} has zero distance")
            if distance > len(output):
                raise FormatError(
                    f"PLE copy block {block_count} distance {distance} exceeds output size {len(output)}"
                )
            source = len(output) - distance
            for _ in range(word_count):
                # REP MOVSW reads a complete word before writing it.  This also
                # rejects distance=1, whose second source byte is not decoded.
                if source < 0 or source + 2 > len(output):
                    raise FormatError(
                        f"PLE copy block {block_count} reads undecoded bytes at source {source}"
                    )
                word = bytes(output[source : source + 2])
                output.extend(word)
                source += 2
            copy_blocks += 1
            copy_words += word_count
            maximum_copy_distance = max(maximum_copy_distance, distance)
        else:
            if cursor + byte_count > len(data):
                raise FormatError(
                    f"truncated PLE literal block {block_count}: need {byte_count} bytes at {cursor}"
                )
            output.extend(data[cursor : cursor + byte_count])
            cursor += byte_count
            literal_blocks += 1
            literal_words += word_count

    trailing_bytes = len(data) - cursor
    if require_complete_input and trailing_bytes:
        raise FormatError(f"PLE stream has {trailing_bytes} trailing byte(s) after its terminator")
    stats = DecompressionStats(
        input_bytes=len(data),
        output_bytes=len(output),
        block_count=block_count,
        literal_blocks=literal_blocks,
        copy_blocks=copy_blocks,
        literal_words=literal_words,
        copy_words=copy_words,
        maximum_copy_distance=maximum_copy_distance,
        bytes_consumed=cursor,
        trailing_bytes=trailing_bytes,
        terminator_found=terminator_found,
    )
    return bytes(output), stats


def parse_pt1(
    data: bytes,
    *,
    max_records: int = DEFAULT_MAX_PT1_RECORDS,
    max_record_payload_bytes: int = DEFAULT_MAX_RECORD_PAYLOAD_BYTES,
    max_total_payload_bytes: int = DEFAULT_MAX_TOTAL_DECOMPRESSED_BYTES,
) -> Pt1ParseResult:
    """Parse concatenated PT1 records ending at EOF or one final zero word."""

    if max_records <= 0 or max_record_payload_bytes < 0 or max_total_payload_bytes < 0:
        raise ValueError("PT1 limits must be positive (byte limits may be zero)")
    if not data:
        raise FormatError("PT1 data contains no records")

    records: list[Pt1Record] = []
    cursor = 0
    total_payload = 0
    terminator = "eof"
    while cursor < len(data):
        remaining = len(data) - cursor
        attribute = struct.unpack_from("<H", data, cursor)[0] if remaining >= 2 else None
        if attribute == 0:
            if remaining != 2:
                raise FormatError("PT1 zero terminator must be the final two bytes")
            cursor += 2
            terminator = "explicit_zero"
            break
        if remaining < 12:
            raise FormatError(f"truncated PT1 header at byte {cursor}: only {remaining} byte(s) remain")
        if len(records) >= max_records:
            raise FormatError(f"PT1 record count exceeds limit {max_records}")

        record_offset = cursor
        attribute, author_x, author_y, width_bytes, height_pixels, payload_length = struct.unpack_from(
            "<6H", data, cursor
        )
        cursor += 12
        if attribute not in (1, 2, 3, 4):
            raise FormatError(f"PT1 record {len(records)} has unsupported attribute {attribute}")
        if payload_length > max_record_payload_bytes:
            raise FormatError(
                f"PT1 record {len(records)} payload {payload_length} exceeds limit {max_record_payload_bytes}"
            )
        if payload_length > len(data) - cursor:
            raise FormatError(
                f"PT1 record {len(records)} payload is truncated: declared {payload_length}, "
                f"available {len(data) - cursor}"
            )
        if attribute in (1, 2, 3) and (width_bytes == 0 or height_pixels == 0):
            raise FormatError(f"PT1 image record {len(records)} has zero dimensions")
        total_payload += payload_length
        if total_payload > max_total_payload_bytes:
            raise FormatError(f"PT1 payload total exceeds limit {max_total_payload_bytes}")
        payload = bytes(data[cursor : cursor + payload_length])
        cursor += payload_length
        records.append(
            Pt1Record(
                index=len(records),
                record_offset=record_offset,
                attribute=attribute,
                author_x_raw=author_x,
                author_y_raw=author_y,
                width_bytes=width_bytes,
                height_pixels=height_pixels,
                payload_length=payload_length,
                payload=payload,
            )
        )

    if not records:
        raise FormatError("PT1 data contains no image or binary records")
    return Pt1ParseResult(records=tuple(records), terminator=terminator, bytes_consumed=cursor)


def decode_record_payload(
    record: Pt1Record,
    *,
    expected_binary_size: int | None = None,
    attribute3_plane_count: int = 1,
    max_output_bytes: int = DEFAULT_MAX_DECOMPRESSED_RECORD_BYTES,
) -> bytes:
    """Decode a PT1 payload and enforce the size implied by its attribute."""

    if attribute3_plane_count not in (1, 4):
        raise ValueError("attribute3_plane_count must be 1 or 4")

    if record.attribute == 1:
        decoded = record.payload
        expected_size = record.plane_size * 4
    elif record.attribute == 2:
        decoded, _ = decompress_ple(record.payload, max_output_bytes=max_output_bytes)
        expected_size = record.plane_size * 4
    elif record.attribute == 3:
        decoded, _ = decompress_ple(record.payload, max_output_bytes=max_output_bytes)
        # Activity sprite attr-3 records are one-plane masks.  At least one
        # legacy J0 entry has a four-plane-sized attr-3 payload; callers must
        # opt into that size only after establishing its role independently.
        expected_size = record.plane_size * attribute3_plane_count
    elif record.attribute == 4:
        decoded, _ = decompress_ple(record.payload, max_output_bytes=max_output_bytes)
        expected_size = expected_binary_size
    else:  # Pt1Record may be manually constructed by callers
        raise FormatError(f"unsupported PT1 attribute {record.attribute}")

    if len(decoded) > max_output_bytes:
        raise FormatError(
            f"PT1 record {record.index} decoded size {len(decoded)} exceeds limit {max_output_bytes}"
        )
    if expected_size is not None and len(decoded) != expected_size:
        raise FormatError(
            f"PT1 record {record.index} decoded size {len(decoded)} does not match expected {expected_size}"
        )
    return decoded


def decode_record_planes(
    record: Pt1Record,
    *,
    attribute3_plane_count: int = 1,
    max_output_bytes: int = DEFAULT_MAX_DECOMPRESSED_RECORD_BYTES,
) -> tuple[bytes, ...]:
    """Return packed planes, with legacy attr-3 width requiring explicit opt-in."""

    if record.attribute == 4:
        raise FormatError("attribute 4 is generic binary data, not image planes")
    decoded = decode_record_payload(
        record,
        attribute3_plane_count=attribute3_plane_count,
        max_output_bytes=max_output_bytes,
    )
    plane_size = record.plane_size
    plane_count = attribute3_plane_count if record.attribute == 3 else 4
    return tuple(
        decoded[index * plane_size : (index + 1) * plane_size]
        for index in range(plane_count)
    )


def planes_to_indices(
    planes: Sequence[bytes],
    width_bytes: int,
    height_pixels: int,
    *,
    msb_left: bool = True,
) -> bytes:
    """Combine four packed one-bit planes into one 0..15 index per pixel.

    PM2 stores each plane in X-byte-major order.  ``PLSLD7.ASM:237-257``
    computes the source offset as ``byte_x * height_pixels + y``; treating the
    buffer as conventional row-major storage transposes groups of eight pixels
    and produces horizontal noise.
    """

    if width_bytes <= 0 or height_pixels <= 0:
        raise ValueError("plane dimensions must be positive")
    if len(planes) != 4:
        raise FormatError(f"four color planes are required, got {len(planes)}")
    plane_size = width_bytes * height_pixels
    if any(len(plane) != plane_size for plane in planes):
        raise FormatError("color plane length does not match the supplied dimensions")

    width_pixels = width_bytes * 8
    output = bytearray(width_pixels * height_pixels)
    for y in range(height_pixels):
        for byte_x in range(width_bytes):
            packed_index = byte_x * height_pixels + y
            for bit_x in range(8):
                bit_mask = 1 << (7 - bit_x if msb_left else bit_x)
                color = 0
                for plane_index, plane in enumerate(planes):
                    if plane[packed_index] & bit_mask:
                        color |= 1 << plane_index
                output[y * width_pixels + byte_x * 8 + bit_x] = color
    return bytes(output)


def _packed_plane_to_bits(
    plane: bytes,
    width_bytes: int,
    height_pixels: int,
    *,
    msb_left: bool,
) -> bytes:
    """Expand one X-byte-major packed plane into row-major one-byte pixels."""

    plane_size = width_bytes * height_pixels
    if len(plane) != plane_size:
        raise FormatError("mask plane length does not match its dimensions")
    output = bytearray(width_bytes * 8 * height_pixels)
    for y in range(height_pixels):
        for byte_x in range(width_bytes):
            value = plane[byte_x * height_pixels + y]
            for bit_x in range(8):
                bit_mask = 1 << (7 - bit_x if msb_left else bit_x)
                output[y * width_bytes * 8 + byte_x * 8 + bit_x] = int(bool(value & bit_mask))
    return bytes(output)


def decode_mask_body_pair(
    mask_record: Pt1Record,
    body_record: Pt1Record,
    *,
    msb_left: bool = True,
    require_transparent_body_zero: bool = True,
    mask_storage_plane_count: int = 1,
    max_output_bytes: int = DEFAULT_MAX_DECOMPRESSED_RECORD_BYTES,
) -> DecodedPattern:
    """Decode one attr-3 mask followed by one attr-2 four-plane body."""

    if mask_record.attribute != 3 or body_record.attribute != 2:
        raise FormatError(
            f"mask/body pair requires attributes 3 then 2, got {mask_record.attribute} then {body_record.attribute}"
        )
    geometry = (
        mask_record.author_x_raw,
        mask_record.author_y_raw,
        mask_record.width_bytes,
        mask_record.height_pixels,
    )
    body_geometry = (
        body_record.author_x_raw,
        body_record.author_y_raw,
        body_record.width_bytes,
        body_record.height_pixels,
    )
    if geometry != body_geometry:
        raise FormatError(f"mask/body geometry differs: mask={geometry}, body={body_geometry}")

    # PARTT2:243-285 -> PLSLD7:208-220,517-559: the mask path
    # reuses the FIRST plane for every destination plane, never NEXTPLNOFS.
    # J007BK's trailing three plane lengths are not per-color-plane masks.
    # Explicit opt-in keeps strict sizes for ordinary sprite data.
    mask_plane = decode_record_planes(mask_record,
        attribute3_plane_count=mask_storage_plane_count,
        max_output_bytes=max_output_bytes)[0]
    body_planes = decode_record_planes(body_record, max_output_bytes=max_output_bytes)
    mask_bits = _packed_plane_to_bits(
        mask_plane,
        mask_record.width_bytes,
        mask_record.height_pixels,
        msb_left=msb_left,
    )
    indices = planes_to_indices(
        body_planes,
        body_record.width_bytes,
        body_record.height_pixels,
        msb_left=msb_left,
    )
    if require_transparent_body_zero:
        violation = next(
            (index for index, (mask, color) in enumerate(zip(mask_bits, indices)) if mask and color),
            None,
        )
        if violation is not None:
            raise FormatError(
                f"body changes a mask-preserved pixel at linear pixel {violation}; conventional alpha is invalid"
            )
    return DecodedPattern(
        width_bytes=mask_record.width_bytes,
        height_pixels=mask_record.height_pixels,
        author_x_raw=mask_record.author_x_raw,
        author_y_raw=mask_record.author_y_raw,
        mask_bits=mask_bits,
        color_indices=indices,
    )


def apply_masked_pattern(
    destination_indices: bytes | bytearray,
    canvas_width: int,
    canvas_height: int,
    pattern: DecodedPattern,
    *,
    anchor_x_pixels: int = 0,
    anchor_y_pixels: int = 0,
    include_author_offset: bool = True,
    clip: bool = False,
) -> bytes:
    """Apply the source mask-AND/body-OR operation to a 4-bit index canvas."""

    if canvas_width <= 0 or canvas_height <= 0:
        raise ValueError("canvas dimensions must be positive")
    if len(destination_indices) != canvas_width * canvas_height:
        raise FormatError("destination index length does not match canvas dimensions")
    if any(value > 15 for value in destination_indices):
        raise FormatError("destination contains a color index greater than 15")
    expected_pattern_size = pattern.width_pixels * pattern.height_pixels
    if len(pattern.mask_bits) != expected_pattern_size or len(pattern.color_indices) != expected_pattern_size:
        raise FormatError("pattern buffers do not match pattern dimensions")
    if any(value not in (0, 1) for value in pattern.mask_bits):
        raise FormatError("pattern mask must contain only zero or one")
    if any(value > 15 for value in pattern.color_indices):
        raise FormatError("pattern contains a color index greater than 15")

    x0 = anchor_x_pixels + (pattern.author_x_pixels if include_author_offset else 0)
    y0 = anchor_y_pixels + (pattern.author_y_pixels if include_author_offset else 0)
    if not clip and (
        x0 < 0
        or y0 < 0
        or x0 + pattern.width_pixels > canvas_width
        or y0 + pattern.height_pixels > canvas_height
    ):
        raise FormatError(
            f"pattern bounds ({x0}, {y0}, {pattern.width_pixels}, {pattern.height_pixels}) "
            f"exceed canvas ({canvas_width}, {canvas_height})"
        )

    output = bytearray(destination_indices)
    for source_y in range(pattern.height_pixels):
        destination_y = y0 + source_y
        if destination_y < 0 or destination_y >= canvas_height:
            continue
        for source_x in range(pattern.width_pixels):
            destination_x = x0 + source_x
            if destination_x < 0 or destination_x >= canvas_width:
                continue
            source_index = source_y * pattern.width_pixels + source_x
            destination_index = destination_y * canvas_width + destination_x
            preserve = 0xF if pattern.mask_bits[source_index] else 0
            output[destination_index] = (
                output[destination_index] & preserve
            ) | pattern.color_indices[source_index]
    return bytes(output)


def parse_palette_parameters(data: bytes) -> PaletteParameters:
    """Parse six groups of eight little-endian words from decoded attr-4 data."""

    if len(data) != 96:
        raise FormatError(f"palette parameter data must be 96 bytes, got {len(data)}")
    values = struct.unpack("<48H", data)
    groups = tuple(tuple(values[start : start + 8]) for start in range(0, 48, 8))
    for label, group, maximum in (
        ("low hue", groups[0], 360),
        ("low saturation", groups[1], 100),
        ("low brightness", groups[2], 100),
        ("high hue", groups[3], 360),
        ("high saturation", groups[4], 100),
        ("high brightness", groups[5], 100),
    ):
        invalid = next((value for value in group if value > maximum), None)
        if invalid is not None:
            raise FormatError(f"{label} value {invalid} exceeds {maximum}")
    return PaletteParameters(*groups)


def indices_to_rgba(
    indices: bytes | bytearray,
    palette: Sequence[Sequence[int]],
) -> bytes:
    """Map indices using an explicitly supplied, non-embedded 16-color palette."""

    if len(palette) != 16:
        raise ValueError("an explicit palette must contain exactly 16 entries")
    normalized: list[tuple[int, int, int, int]] = []
    for index, entry in enumerate(palette):
        if len(entry) not in (3, 4):
            raise ValueError(f"palette entry {index} must contain RGB or RGBA")
        if any(not isinstance(value, int) or value < 0 or value > 255 for value in entry):
            raise ValueError(f"palette entry {index} contains a channel outside 0..255")
        normalized.append(
            (entry[0], entry[1], entry[2], entry[3] if len(entry) == 4 else 255)
        )
    if any(index > 15 for index in indices):
        raise FormatError("color index greater than 15 cannot be mapped by a 16-color palette")
    output = bytearray()
    for index in indices:
        output.extend(normalized[index])
    return bytes(output)


def _summarize_activity_asset(
    payload: bytes,
    metadata: dict[str, int | str],
    *,
    role: str,
) -> dict[str, object]:
    parsed = parse_pt1(payload)
    attribute_counts: dict[str, int] = {}
    geometry_counts: dict[str, int] = {}
    decoded_total_bytes = 0
    compressed_record_count = 0
    compressed_complete_count = 0
    author_x_values: list[int] = []
    author_y_values: list[int] = []
    for record in parsed:
        attribute_key = str(record.attribute)
        attribute_counts[attribute_key] = attribute_counts.get(attribute_key, 0) + 1
        geometry_key = f"{record.width_bytes}x{record.height_pixels}"
        geometry_counts[geometry_key] = geometry_counts.get(geometry_key, 0) + 1
        author_x_values.append(record.author_x_raw)
        author_y_values.append(record.author_y_raw)
        decoded_total_bytes += len(decode_record_payload(record))
        if record.attribute in (2, 3, 4):
            compressed_record_count += 1
            _, stats = decompress_ple(record.payload)
            if stats.terminator_found and stats.trailing_bytes == 0 and stats.bytes_consumed == len(record.payload):
                compressed_complete_count += 1

    pair_count = 0
    pair_geometry_match_count = 0
    transparent_body_violation_pixels = 0
    if role == "pattern":
        if len(parsed) % 2:
            raise FormatError(f"pattern asset {metadata['entry_name']!r} has an odd record count")
        for index in range(0, len(parsed), 2):
            mask_record, body_record = parsed[index], parsed[index + 1]
            pattern = decode_mask_body_pair(
                mask_record,
                body_record,
                require_transparent_body_zero=False,
            )
            pair_count += 1
            pair_geometry_match_count += 1
            transparent_body_violation_pixels += sum(
                1
                for mask_bit, color_index in zip(pattern.mask_bits, pattern.color_indices)
                if mask_bit and color_index
            )

    return {
        "entry_name": metadata["entry_name"],
        "entry_offset": metadata["entry_offset"],
        "entry_length": metadata["entry_length"],
        "entry_sha256": metadata["entry_sha256"],
        "role": role,
        "record_count": len(parsed),
        "attribute_counts": attribute_counts,
        "geometry_counts_width_bytes_x_height_pixels": geometry_counts,
        "pt1_termination": parsed.terminator,
        "pt1_input_fully_consumed": parsed.bytes_consumed == len(payload),
        "decoded_total_bytes": decoded_total_bytes,
        "compressed_record_count": compressed_record_count,
        "compressed_payload_fully_consumed_count": compressed_complete_count,
        "mask_body": {
            "pair_count": pair_count,
            "geometry_match_count": pair_geometry_match_count,
            "transparent_body_violation_pixels": transparent_body_violation_pixels,
        },
        "author_offset": {
            "x_raw_byte_columns_range": [min(author_x_values), max(author_x_values)],
            "x_pixels_range": [min(author_x_values) * 8, max(author_x_values) * 8],
            "y_pixels_range": [min(author_y_values), max(author_y_values)],
            "x_conversion": "author_x_raw * 8",
        },
    }


def _summarize_palette_candidate(
    payload: bytes,
    metadata: dict[str, int | str],
) -> dict[str, object]:
    parsed = parse_pt1(payload)
    decoded_lengths: list[int] = []
    complete_count = 0
    valid_parameter_record_count = 0
    for record in parsed:
        if record.attribute != 4:
            raise FormatError(
                f"palette candidate {metadata['entry_name']!r} contains attribute {record.attribute}, expected 4"
            )
        decoded = decode_record_payload(record, expected_binary_size=96)
        decoded_lengths.append(len(decoded))
        parse_palette_parameters(decoded)
        valid_parameter_record_count += 1
        _, stats = decompress_ple(record.payload)
        if stats.terminator_found and stats.trailing_bytes == 0 and stats.bytes_consumed == len(record.payload):
            complete_count += 1
    return {
        "entry_name": metadata["entry_name"],
        "entry_offset": metadata["entry_offset"],
        "entry_length": metadata["entry_length"],
        "entry_sha256": metadata["entry_sha256"],
        "record_count": len(parsed),
        "attribute_counts": {"4": len(parsed)},
        "decoded_length_set": sorted(set(decoded_lengths)),
        "valid_parameter_record_count": valid_parameter_record_count,
        "compressed_payload_fully_consumed_count": complete_count,
        "allowed_parameter_ranges": {
            "hue": [0, 360],
            "saturation": [0, 100],
            "brightness": [0, 100],
        },
        "exact_parameter_values_in_report": False,
    }


def build_activity_pt1_report(
    archive_path: str | Path,
    *,
    expected_archive_sha256: str,
    source_commit: str = FIXED_SOURCE_COMMIT,
) -> dict[str, object]:
    """Build the plan's safe R3 report without serializing original content."""

    mappings = {
        "JOB003": {
            "resource_stem": "J004",
            "background": "J004B.PT1",
            "pattern": "J004A.PT1",
            "source_evidence": [
                "KOSOTEXT/JOB003.TXT:370",
                "KOSOTEXT/JOB003.TXT:371",
                "KOSO/misc/LIBXX.BAT:937",
                "KOSO/misc/LIBXX.BAT:938",
            ],
        },
        "JOB008": {
            "resource_stem": "J009",
            "background": "J009B.PT1",
            "pattern": "J009A.PT1",
            "source_evidence": [
                "KOSOTEXT/JOB008.TXT:401",
                "KOSOTEXT/JOB008.TXT:402",
                "KOSO/misc/LIBXX.BAT:948",
                "KOSO/misc/LIBXX.BAT:949",
            ],
        },
    }
    assets: dict[str, object] = {}
    library_metadata: dict[str, object] | None = None
    for mapping in mappings.values():
        for role in ("pattern", "background"):
            asset_name = str(mapping[role])
            payload, metadata = read_lbx_asset_from_zip(
                archive_path,
                "J0.LBX",
                asset_name,
                expected_archive_sha256=expected_archive_sha256,
            )
            if library_metadata is None:
                library_metadata = {
                    "member": metadata["library_member"],
                    "length": metadata["library_length"],
                    "sha256": metadata["library_sha256"],
                }
            assets[asset_name] = _summarize_activity_asset(payload, metadata, role=role)

    palette_candidates: dict[str, object] = {}
    for library, asset in (
        ("OP.LBX", "OPNPALET.PT1"),
        ("EN.LBX", "ENDPALET.PT1"),
    ):
        payload, metadata = read_lbx_asset_from_zip(
            archive_path,
            library,
            asset,
            expected_archive_sha256=expected_archive_sha256,
        )
        palette_candidates[f"{library}/{asset}"] = _summarize_palette_candidate(payload, metadata)

    return {
        "schema_version": 1,
        "report_kind": "pm2_activity_pt1_safe_structural_probe",
        "tool": {"path": "work/reference_analysis/pm2_activities/pm2_lbx_pt1.py", "version": TOOL_VERSION},
        "fixed_inputs": {
            "source_commit": source_commit,
            "archive_sha256": expected_archive_sha256.lower(),
        },
        "scope": {
            "scripts": ["JOB003", "JOB008"],
            "library": library_metadata,
            "mapping_basis": "S1 explicit WWANIME resource loads; not inferred from equal numbering",
            "job_resource_mappings": mappings,
        },
        "activity_assets": assets,
        "palette_candidates": palette_candidates,
        "format_findings": {
            "plane_order": "plane_0_to_plane_3_combined_as_bits_0_to_3_from_S1",
            "storage_order": "x_byte_major_packed_index_equals_byte_x_times_height_pixels_plus_y",
            "horizontal_bit_order": "msb_left_candidate_pending_R5_runtime_confirmation",
            "mask_body_operation": "destination = (destination AND mask) OR body",
            "author_x_unit": "eight_pixel_byte_column",
            "author_y_unit": "pixel_row",
            "legacy_attribute_3": "J007BK.PT1 outside target scope has four-plane-sized attr3; role remains unresolved",
        },
        "unresolved": [
            "JOB003/JOB008 active palette record and CLRCDE state in the fixed DOS run",
            "final RGB values until the runtime palette state is bound",
            "MSB-left visual orientation until R5 comparison with captured frames",
            "tick-to-captured-frame timing until R1/R5",
        ],
        "safety": {
            "contains_original_payload": False,
            "contains_decoded_pixels_or_planes": False,
            "contains_exact_palette_parameters": False,
            "contains_original_text": False,
            "game_build_input": False,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a safe structural report for the fixed JOB003/JOB008 PM2 PT1 scope."
    )
    parser.add_argument("--archive", required=True, help="Path to the fixed read-only PM2 ZIP/DOSZ")
    parser.add_argument(
        "--expected-archive-sha256",
        default=FIXED_ARCHIVE_SHA256,
        help="Required archive identity (defaults to the v0.1 plan hash)",
    )
    parser.add_argument("--source-commit", default=FIXED_SOURCE_COMMIT)
    parser.add_argument("--output", default="-", help="Safe JSON output path, or '-' for stdout")
    args = parser.parse_args(argv)

    try:
        report = build_activity_pt1_report(
            args.archive,
            expected_archive_sha256=args.expected_archive_sha256,
            source_commit=args.source_commit,
        )
        serialized = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output == "-":
            print(serialized, end="")
        else:
            output_path = Path(args.output)
            _atomic_write_text(output_path, serialized)
    except (
        FormatError,
        OSError,
        RuntimeError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
