from __future__ import annotations

import hashlib
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from pm2_animation_lab.pm2_lbx_pt1 import (
    DecodedPattern,
    FormatError,
    apply_masked_pattern,
    build_activity_pt1_report,
    decode_mask_body_pair,
    decode_record_payload,
    decode_record_planes,
    decompress_ple,
    indices_to_rgba,
    parse_lbx_directory,
    parse_palette_parameters,
    parse_pt1,
    planes_to_indices,
    read_lbx_asset_from_zip,
)


def expect_raises(exception_type, function, contains: str | None = None) -> None:
    try:
        function()
    except exception_type as exc:
        if contains is not None:
            assert contains.lower() in str(exc).lower(), str(exc)
    else:
        raise AssertionError(f"expected {exception_type.__name__}")


def make_lbx(entries: list[tuple[str, bytes]]) -> bytes:
    payload = bytearray()
    directory_rows = bytearray()
    for name, data in entries:
        offset = len(payload)
        payload.extend(data)
        directory_rows.extend(name.encode("ascii").ljust(12, b" "))
        directory_rows.extend(struct.pack("<II", offset, len(data)))
    directory_offset = len(payload)
    return bytes(payload + directory_rows + struct.pack("<HI", len(entries), directory_offset))


def make_pt1_record(
    attribute: int,
    payload: bytes,
    *,
    author_x: int = 0,
    author_y: int = 0,
    width_bytes: int = 1,
    height_pixels: int = 1,
) -> bytes:
    return struct.pack(
        "<6H", attribute, author_x, author_y, width_bytes, height_pixels, len(payload)
    ) + payload


def ple_literal(data: bytes) -> bytes:
    assert len(data) % 2 == 0 and len(data) // 2 < 0x8000
    return struct.pack("<H", len(data) // 2) + data + b"\0\0"


def check_lbx_and_zip() -> None:
    lbx = make_lbx([("FIRST.PT1", b"first"), ("SECOND.PT1", b"second")])
    entries = parse_lbx_directory(lbx)
    assert [(entry.name, entry.offset, entry.length) for entry in entries] == [
        ("FIRST.PT1", 0, 5),
        ("SECOND.PT1", 5, 6),
    ]
    expect_raises(FormatError, lambda: parse_lbx_directory(b"short"), "footer")
    expect_raises(FormatError, lambda: parse_lbx_directory(lbx, max_entries=1), "exceeds")

    duplicate = make_lbx([("SAME.PT1", b"a"), ("same.pt1", b"b")])
    expect_raises(FormatError, lambda: parse_lbx_directory(duplicate), "duplicate")

    bad_directory = bytearray(lbx)
    struct.pack_into("<I", bad_directory, len(bad_directory) - 4, len(lbx) - 7)
    expect_raises(FormatError, lambda: parse_lbx_directory(bytes(bad_directory)), "directory")

    overlapping = bytearray(lbx)
    directory_offset = struct.unpack_from("<I", overlapping, len(overlapping) - 4)[0]
    struct.pack_into("<II", overlapping, directory_offset + 20 + 12, 4, 6)
    expect_raises(FormatError, lambda: parse_lbx_directory(bytes(overlapping)), "overlap")

    directory_collision = bytearray(lbx)
    struct.pack_into("<II", directory_collision, directory_offset + 12, directory_offset - 1, 2)
    expect_raises(FormatError, lambda: parse_lbx_directory(bytes(directory_collision)), "directory")

    bad_name = bytearray(lbx)
    bad_name[directory_offset] = 0
    expect_raises(FormatError, lambda: parse_lbx_directory(bytes(bad_name)), "name")

    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        archive_path = root / "fixture.zip"
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("fixture/PM2/J0.LBX", lbx)
        before = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        payload, metadata = read_lbx_asset_from_zip(
            archive_path,
            "J0.LBX",
            "SECOND.PT1",
            expected_archive_sha256=before,
        )
        after = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        assert payload == b"second"
        assert metadata["entry_sha256"] == hashlib.sha256(b"second").hexdigest()
        assert before == after == metadata["archive_sha256"]
        expect_raises(
            FormatError,
            lambda: read_lbx_asset_from_zip(
                archive_path, "J0.LBX", "SECOND.PT1", expected_archive_sha256="0" * 64
            ),
            "sha-256 mismatch",
        )
        expect_raises(
            FormatError,
            lambda: read_lbx_asset_from_zip(
                archive_path, "J0.LBX", "SECOND.PT1", max_zip_member_bytes=1
            ),
            "exceeds limit",
        )

        encrypted_path = root / "encrypted-flag.zip"
        with zipfile.ZipFile(encrypted_path, "w") as archive:
            archive.writestr("J0.LBX", lbx)
        encrypted_bytes = bytearray(encrypted_path.read_bytes())
        local_header = encrypted_bytes.index(b"PK\x03\x04")
        central_header = encrypted_bytes.index(b"PK\x01\x02")
        local_flags = struct.unpack_from("<H", encrypted_bytes, local_header + 6)[0] | 1
        central_flags = struct.unpack_from("<H", encrypted_bytes, central_header + 8)[0] | 1
        struct.pack_into("<H", encrypted_bytes, local_header + 6, local_flags)
        struct.pack_into("<H", encrypted_bytes, central_header + 8, central_flags)
        encrypted_path.write_bytes(encrypted_bytes)
        expect_raises(
            FormatError,
            lambda: read_lbx_asset_from_zip(encrypted_path, "J0.LBX", "FIRST.PT1"),
            "encrypted",
        )

        ambiguous_path = root / "ambiguous.zip"
        with zipfile.ZipFile(ambiguous_path, "w") as archive:
            archive.writestr("one/J0.LBX", lbx)
            archive.writestr("two/J0.LBX", lbx)
        expect_raises(
            FormatError,
            lambda: read_lbx_asset_from_zip(ambiguous_path, "J0.LBX", "FIRST.PT1"),
            "ambiguous",
        )


def check_ple() -> None:
    literal, stats = decompress_ple(ple_literal(b"ABCD"))
    assert literal == b"ABCD"
    assert stats.literal_blocks == 1 and stats.literal_words == 2 and stats.trailing_bytes == 0

    overlapping_copy = (
        struct.pack("<H", 1)
        + b"AB"
        + struct.pack("<HHH", 0x8002, 2, 0)
    )
    decoded, stats = decompress_ple(overlapping_copy)
    assert decoded == b"ABABAB"
    assert stats.copy_blocks == 1 and stats.copy_words == 2 and stats.maximum_copy_distance == 2

    expect_raises(FormatError, lambda: decompress_ple(b"\x01"), "truncated")
    expect_raises(
        FormatError,
        lambda: decompress_ple(struct.pack("<H", 2) + b"AB"),
        "literal",
    )
    expect_raises(
        FormatError,
        lambda: decompress_ple(struct.pack("<HHH", 0x8001, 0, 0)),
        "zero distance",
    )
    expect_raises(
        FormatError,
        lambda: decompress_ple(struct.pack("<HHH", 0x8001, 2, 0)),
        "exceeds output",
    )
    distance_one = struct.pack("<H", 1) + b"AB" + struct.pack("<HHH", 0x8001, 1, 0)
    expect_raises(FormatError, lambda: decompress_ple(distance_one), "undecoded")
    expect_raises(FormatError, lambda: decompress_ple(b"\0\0X"), "trailing")
    partial, partial_stats = decompress_ple(b"\0\0X", require_complete_input=False)
    assert partial == b"" and partial_stats.trailing_bytes == 1
    expect_raises(FormatError, lambda: decompress_ple(ple_literal(b"AB"), max_output_bytes=1), "limit")


def check_pt1_and_planes() -> None:
    raw_planes = bytes((0x80, 0x40, 0x20, 0x10))
    attr1 = make_pt1_record(1, raw_planes, author_x=2, author_y=3)
    attr2 = make_pt1_record(2, ple_literal(raw_planes), author_x=2, author_y=3)
    attr3 = make_pt1_record(3, ple_literal(bytes((0x7F,)) + b"\0"), width_bytes=1, height_pixels=2)
    # The attr-3 fixture has two rows so its one-byte-per-row decoded length is even.
    result = parse_pt1(attr1 + attr2 + attr3)
    assert result.terminator == "eof" and len(result.records) == 3
    assert len(result) == 3 and result[0] is result.records[0]
    assert result.records[0].author_x_raw == 2
    assert result.records[0].author_x_pixels == 16
    assert result.records[0].author_y_pixels == 3
    assert result.records[0].width_pixels == 8
    assert decode_record_payload(result.records[0]) == raw_planes
    assert decode_record_payload(result.records[1]) == raw_planes
    assert decode_record_planes(result.records[0]) == tuple(bytes((value,)) for value in raw_planes)
    indices = planes_to_indices(decode_record_planes(result.records[0]), 1, 1)
    assert indices == bytes((1, 2, 4, 8, 0, 0, 0, 0))
    reversed_bits = planes_to_indices(decode_record_planes(result.records[0]), 1, 1, msb_left=False)
    assert reversed_bits == bytes((0, 0, 0, 0, 8, 4, 2, 1))

    explicit = parse_pt1(attr1 + b"\0\0")
    assert explicit.terminator == "explicit_zero" and explicit.bytes_consumed == len(attr1) + 2
    expect_raises(FormatError, lambda: parse_pt1(attr1 + b"\0\0X"), "terminator")
    expect_raises(FormatError, lambda: parse_pt1(attr1 + b"X"), "truncated")
    expect_raises(FormatError, lambda: parse_pt1(make_pt1_record(9, b"")), "unsupported")
    expect_raises(ValueError, lambda: parse_pt1(attr1, max_records=0), "limits")
    expect_raises(FormatError, lambda: parse_pt1(attr1 + attr1, max_records=1), "count")

    wrong_record = parse_pt1(make_pt1_record(1, b"1234", height_pixels=2)).records[0]
    expect_raises(FormatError, lambda: decode_record_payload(wrong_record), "decoded size")
    expect_raises(
        FormatError,
        lambda: decode_record_payload(result.records[0], max_output_bytes=3),
        "exceeds limit",
    )

    legacy_attr3 = parse_pt1(
        make_pt1_record(3, ple_literal(raw_planes), width_bytes=1, height_pixels=1)
    ).records[0]
    expect_raises(
        FormatError,
        lambda: decode_record_payload(legacy_attr3),
        "decoded size",
    )
    assert decode_record_planes(legacy_attr3, attribute3_plane_count=4) == tuple(
        bytes((value,)) for value in raw_planes
    )
    expect_raises(
        ValueError,
        lambda: decode_record_payload(legacy_attr3, attribute3_plane_count=2),
        "must be 1 or 4",
    )

    binary_data = bytes(range(96))
    binary_record = parse_pt1(make_pt1_record(4, ple_literal(binary_data), width_bytes=0, height_pixels=0)).records[0]
    assert decode_record_payload(binary_record, expected_binary_size=96) == binary_data
    expect_raises(FormatError, lambda: decode_record_planes(binary_record), "binary")


def check_x_byte_major_storage_order() -> None:
    """Distinguish PM2's X-byte-major planes from conventional row-major data."""

    width_bytes = 2
    height_pixels = 2
    # Source byte order is (byte_x=0,y=0), (0,1), (1,0), (1,1).
    plane_zero = bytes((0x80, 0x40, 0x20, 0x10))
    empty_plane = bytes(4)
    indices = planes_to_indices(
        (plane_zero, empty_plane, empty_plane, empty_plane),
        width_bytes,
        height_pixels,
    )
    expected = bytearray(16 * 2)
    expected[0] = 1
    expected[10] = 1
    expected[16 + 1] = 1
    expected[16 + 11] = 1
    assert indices == bytes(expected)

    # The paired mask uses the same storage order.  Each zero mask bit aligns
    # with the corresponding plane-zero body pixel, so strict alpha validation
    # also detects a layout mismatch between mask and body expansion.
    mask_plane = bytes((0x7F, 0xBF, 0xDF, 0xEF))
    mask_record = make_pt1_record(
        3,
        ple_literal(mask_plane),
        width_bytes=width_bytes,
        height_pixels=height_pixels,
    )
    body_record = make_pt1_record(
        2,
        ple_literal(plane_zero + empty_plane * 3),
        width_bytes=width_bytes,
        height_pixels=height_pixels,
    )
    mask, body = parse_pt1(mask_record + body_record).records
    pattern = decode_mask_body_pair(mask, body)
    opaque_positions = [
        index for index, value in enumerate(pattern.opaque_bits) if value
    ]
    color_positions = [
        index for index, value in enumerate(pattern.color_indices) if value
    ]
    assert opaque_positions == [0, 10, 17, 27]
    assert color_positions == opaque_positions


def check_mask_composition_and_palette() -> None:
    mask = make_pt1_record(3, ple_literal(b"\x7f\xff"), author_x=1, author_y=1, height_pixels=2)
    body_planes = b"\x80\x00" + b"\x00\x00" * 3
    body = make_pt1_record(2, ple_literal(body_planes), author_x=1, author_y=1, height_pixels=2)
    records = parse_pt1(mask + body).records
    pattern = decode_mask_body_pair(records[0], records[1])
    assert pattern.width_pixels == 8 and pattern.height_pixels == 2
    assert pattern.mask_bits[:8] == bytes((0, 1, 1, 1, 1, 1, 1, 1))
    assert pattern.opaque_bits[:8] == bytes((1, 0, 0, 0, 0, 0, 0, 0))
    assert pattern.color_indices[:8] == bytes((1, 0, 0, 0, 0, 0, 0, 0))

    canvas = bytes((6,)) * (20 * 4)
    composed = apply_masked_pattern(
        canvas,
        20,
        4,
        pattern,
        anchor_x_pixels=1,
        anchor_y_pixels=0,
    )
    # External X=1 plus author byte-column X=1 (8 px), Y=0+1.
    assert composed[1 * 20 + 9] == 1
    assert composed[1 * 20 + 10] == 6

    clipped_pattern = DecodedPattern(1, 1, 0, 0, bytes((0,)) * 8, bytes((2,)) * 8)
    expect_raises(
        FormatError,
        lambda: apply_masked_pattern(canvas, 20, 4, clipped_pattern, anchor_x_pixels=18),
        "exceed canvas",
    )
    clipped = apply_masked_pattern(
        canvas, 20, 4, clipped_pattern, anchor_x_pixels=18, clip=True
    )
    assert clipped[18:20] == b"\x02\x02"

    bad_body_planes = b"\xc0\x00" + b"\x00\x00" * 3
    bad_body = make_pt1_record(2, ple_literal(bad_body_planes), author_x=1, author_y=1, height_pixels=2)
    bad_record = parse_pt1(mask + bad_body).records[1]
    expect_raises(
        FormatError,
        lambda: decode_mask_body_pair(records[0], bad_record),
        "mask-preserved",
    )

    low_hue = tuple(range(8))
    low_sat = tuple(10 + value for value in range(8))
    low_value = tuple(20 + value for value in range(8))
    high_hue = tuple(100 + value for value in range(8))
    high_sat = tuple(30 + value for value in range(8))
    high_value = tuple(40 + value for value in range(8))
    palette_data = struct.pack(
        "<48H", *(low_hue + low_sat + low_value + high_hue + high_sat + high_value)
    )
    palette_parameters = parse_palette_parameters(palette_data)
    slots = palette_parameters.hardware_slots()
    assert slots[0] == (0, 10, 20)
    assert slots[9] == (1, 11, 21)
    assert slots[8] == (100, 30, 40)
    assert slots[1] == (101, 31, 41)
    expect_raises(FormatError, lambda: parse_palette_parameters(b"short"), "96 bytes")
    invalid_palette = bytearray(palette_data)
    struct.pack_into("<H", invalid_palette, 0, 361)
    expect_raises(FormatError, lambda: parse_palette_parameters(bytes(invalid_palette)), "exceeds")

    explicit_palette = [(value, 0, 255 - value) for value in range(16)]
    rgba = indices_to_rgba(bytes((0, 15)), explicit_palette)
    assert rgba == bytes((0, 0, 255, 255, 15, 0, 240, 255))


def check_safe_structural_report() -> None:
    mask_record = make_pt1_record(3, ple_literal(b"\x7f\xff"), height_pixels=2)
    body_record = make_pt1_record(
        2,
        ple_literal(b"\x80\x00" + b"\x00\x00" * 3),
        height_pixels=2,
    )
    background_record = make_pt1_record(2, ple_literal(b"\0" * 4))
    palette_record = make_pt1_record(
        4,
        ple_literal(b"\0" * 96),
        width_bytes=0,
        height_pixels=0,
    )
    j0_lbx = make_lbx(
        [
            ("J004A.PT1", mask_record + body_record),
            ("J004B.PT1", background_record),
            ("J009A.PT1", mask_record + body_record),
            ("J009B.PT1", background_record),
        ]
    )
    op_lbx = make_lbx([("OPNPALET.PT1", palette_record)])
    en_lbx = make_lbx([("ENDPALET.PT1", palette_record)])
    with tempfile.TemporaryDirectory() as temporary:
        archive_path = Path(temporary) / "report-fixture.zip"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("fixture/J0.LBX", j0_lbx)
            archive.writestr("fixture/OP.LBX", op_lbx)
            archive.writestr("fixture/EN.LBX", en_lbx)
        archive_sha256 = hashlib.sha256(archive_path.read_bytes()).hexdigest()
        report = build_activity_pt1_report(
            archive_path,
            expected_archive_sha256=archive_sha256,
            source_commit="synthetic-fixture",
        )
    assert report["scope"]["job_resource_mappings"]["JOB003"]["resource_stem"] == "J004"
    assert report["format_findings"]["storage_order"] == (
        "x_byte_major_packed_index_equals_byte_x_times_height_pixels_plus_y"
    )
    assert report["activity_assets"]["J004A.PT1"]["mask_body"]["pair_count"] == 1
    assert (
        report["activity_assets"]["J004A.PT1"]["compressed_payload_fully_consumed_count"]
        == 2
    )
    assert report["palette_candidates"]["OP.LBX/OPNPALET.PT1"]["valid_parameter_record_count"] == 1
    assert report["safety"] == {
        "contains_original_payload": False,
        "contains_decoded_pixels_or_planes": False,
        "contains_exact_palette_parameters": False,
        "contains_original_text": False,
        "game_build_input": False,
    }
    serialized = str(report)
    assert "low_hue" not in serialized and "color_indices" not in serialized and "mask_bits" not in serialized


class Pm2LbxPt1Tests(unittest.TestCase):
    def test_extended_mask_storage_uses_one_mask_for_all_colors(self) -> None:
        first = b'\x7f\xff'
        # Different trailing data catches an incorrect four-plane-mask model.
        payload = first + b'\x00\x00\xff\xff\x55\xaa'
        mask = make_pt1_record(3, ple_literal(payload), height_pixels=2)
        body = make_pt1_record(2, ple_literal(b'\x80\x00' + b'\x00\x00'*3), height_pixels=2)
        records = parse_pt1(mask+body).records
        with self.assertRaises(FormatError):
            decode_mask_body_pair(*records)
        pattern = decode_mask_body_pair(*records, mask_storage_plane_count=4)
        result = apply_masked_pattern(bytes([14])*16, 8, 2, pattern)
        self.assertEqual(result, bytes([1]+[14]*15))

    def test_lbx_and_zip(self) -> None:
        check_lbx_and_zip()

    def test_ple(self) -> None:
        check_ple()

    def test_pt1_and_planes(self) -> None:
        check_pt1_and_planes()

    def test_x_byte_major_storage_order(self) -> None:
        check_x_byte_major_storage_order()

    def test_mask_composition_and_palette(self) -> None:
        check_mask_composition_and_palette()

    def test_safe_structural_report(self) -> None:
        check_safe_structural_report()


def run() -> None:
    check_lbx_and_zip()
    check_ple()
    check_pt1_and_planes()
    check_x_byte_major_storage_order()
    check_mask_composition_and_palette()
    check_safe_structural_report()
    print(
        "PM2 LBX/PT1 tests passed: strict LBX/ZIP bounds, PLE decoding, PT1 records, "
        "X-byte-major planar indices, mask/body composition, author offsets, and palette parameters."
    )


if __name__ == "__main__":
    run()
