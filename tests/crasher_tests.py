# -*- coding: utf-8 -*-
"""The check for fonts FontLab 7.2 crashes on (a GPOS single adjustment with no values) and the fixed
copy open_font opens instead, on tiny fonts built here (fontbytes); Python 2.7 and 3."""

from __future__ import absolute_import, division, print_function

import hashlib
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "listener"))
os.environ["FREELAB_NO_AUTOSTART"] = "1"

import fontbytes as fb  # noqa: E402
import freelab_listener as fl  # noqa: E402

EMPTY = fb.single(2, 0, 0)  # the crasher: format 2, no values
EMPTY_FOUR = fb.single(2, 4, 0, glyphs=(1, 2, 3, 4, 5, 6, 7), ranged=True)  # (ValueFormat 4, a ranged coverage)


def crasher_gpos():
    """Lookup 0 a pair adjustment; lookup 1 type 1 with a format 1, an empty format 2, and a full format 2;
    lookup 2 an extension of an empty format 2; lookup 3 an extension of a pair adjustment."""
    return fb.gpos([
        fb.lookup(2, [fb.pair()]),
        fb.lookup(1, [fb.single(1, 4), EMPTY, fb.single(2, 4, 3)]),
        fb.lookup(9, [fb.extension(EMPTY_FOUR)]),
        fb.lookup(9, [fb.extension(fb.pair(), kind=2)]),
    ])


def clean_gpos():
    return fb.gpos([fb.lookup(1, [fb.single(1, 0), fb.single(2, 0, 3), fb.single(2, 4, 1)]),
                    fb.lookup(9, [fb.extension(fb.single(2, 5, 2))])])


def font(gpos=None, version=b"\x00\x01\x00\x00"):
    tables = {b"head": fb.head(), b"name": b"\0\0\0\0\0\x06" + b"x" * 7}
    if gpos is not None:
        tables[b"GPOS"] = gpos
    return tables


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="freelab-crasher-")
        self._brotli = fl._brotli_decompress

    def tearDown(self):
        fl._brotli_decompress = self._brotli
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, name, data):
        path = os.path.join(self.tmp, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def found(self, scan):
        return [(entry["font"], s["lookup"], s["subtable"], s["extension"], s["value_format"], s["glyphs"])
                for entry in scan["fonts"] for s in entry["subtables"]]


class DetectionTests(Case):
    def test_the_walker_finds_format_2_with_no_values_only(self):
        found = fl.empty_single_adjustments(crasher_gpos())
        self.assertEqual([(f["lookup"], f["subtable"], f["extension"], f["value_format"], f["glyphs"]) for f in found],
                         [(1, 1, False, 0, 3), (2, 0, True, 4, 7)])
        self.assertEqual(fl.empty_single_adjustments(clean_gpos()), [], "format 1, and format 2 with values: none")
        self.assertEqual(fl.empty_single_adjustments(fb.gpos([])), [])

    def test_an_sfnt(self):
        scan = fl.scan_font_file(self.write("Crash.ttf", fb.sfnt(font(crasher_gpos()))))
        self.assertEqual((scan["format"], scan["checked"], scan["count"]), ("sfnt", True, 2))
        self.assertEqual(self.found(scan), [(0, 1, 1, False, 0, 3), (0, 2, 0, True, 4, 7)])
        self.assertEqual(fl.crasher_summary(scan),
                         u"2 single adjustment subtables with no values (lookups 1 and 2)")
        otf = fl.scan_font_file(self.write("Clean.otf", fb.sfnt(font(clean_gpos()), b"OTTO")))
        self.assertEqual((otf["format"], otf["checked"], otf["count"], otf["fonts"]), ("sfnt", True, 0, []))
        no_gpos = fl.scan_font_file(self.write("NoGpos.ttf", fb.sfnt(font())))
        self.assertEqual((no_gpos["checked"], no_gpos["count"]), (True, 0))

    def test_one_subtable_is_said_in_the_singular(self):
        scan = fl.scan_font_file(self.write("One.ttf", fb.sfnt(font(fb.gpos([fb.lookup(1, [EMPTY])])))))
        self.assertEqual(fl.crasher_summary(scan), u"1 single adjustment subtable with no values (lookup 0)")
        three = fb.gpos([fb.lookup(1, [EMPTY]), fb.lookup(1, [EMPTY, EMPTY]), fb.lookup(1, [EMPTY])])
        scan = fl.scan_font_file(self.write("Three.ttf", fb.sfnt(font(three))))
        self.assertEqual(fl.crasher_summary(scan),
                         u"4 single adjustment subtables with no values (lookups 0, 1, and 2)")

    def test_every_font_of_a_collection_is_checked(self):
        data = fb.collection([font(clean_gpos()), font(crasher_gpos()), font(fb.gpos([fb.lookup(1, [EMPTY])]))])
        scan = fl.scan_font_file(self.write("Family.ttc", data))
        self.assertEqual((scan["format"], scan["checked"], scan["count"]), ("collection", True, 3))
        self.assertEqual([entry["font"] for entry in scan["fonts"]], [1, 2])
        self.assertEqual(fl.crasher_summary(scan), u"3 single adjustment subtables with no values "
                                                   u"(font 1: lookups 1 and 2; font 2: lookup 0)")
        clean = fl.scan_font_file(self.write("Clean.otc", fb.collection([font(clean_gpos()), font()])))
        self.assertEqual((clean["checked"], clean["count"]), (True, 0))

    def test_a_woff(self):
        for compress in (True, False):
            scan = fl.scan_font_file(self.write("Crash.woff", fb.woff(font(crasher_gpos()), compress=compress)))
            self.assertEqual((scan["format"], scan["checked"], scan["count"]), ("woff", True, 2))
        clean = fl.scan_font_file(self.write("Clean.woff", fb.woff(font(clean_gpos()))))
        self.assertEqual((clean["checked"], clean["count"]), (True, 0))

    def test_a_woff2_with_brotli_and_without(self):
        path = self.write("Crash.woff2", fb.woff2(font(crasher_gpos())))
        fl._brotli_decompress = lambda: None
        scan = fl.scan_font_file(path)
        self.assertEqual((scan["format"], scan["checked"], scan["count"]), ("woff2", False, 0))
        self.assertIn("brotli", scan["why_not"])
        fl._brotli_decompress = lambda: (lambda data: data)  # (the stream fontbytes writes is not compressed)
        scan = fl.scan_font_file(path)
        self.assertEqual((scan["checked"], scan["count"]), (True, 2))
        self.assertEqual(self.found(scan), [(0, 1, 1, False, 0, 3), (0, 2, 0, True, 4, 7)])
        clean = fl.scan_font_file(self.write("Clean.woff2", fb.woff2(font(clean_gpos()))))
        self.assertEqual((clean["checked"], clean["count"]), (True, 0))
        # A WOFF2 collection: font 0 has the clean GPOS, font 1 the crasher's (tables 0 to 3, head shared)
        tables = [(b"head", fb.head()), (b"GPOS", clean_gpos()), (b"GPOS", crasher_gpos()), (b"Zzzz", b"zz")]
        scan = fl.scan_font_file(self.write("Family.woff2", fb.woff2(tables, collection_of=[[0, 1, 3], [0, 2]])))
        self.assertEqual([entry["font"] for entry in scan["fonts"]], [1])
        self.assertEqual(fl.crasher_summary(scan), u"2 single adjustment subtables with no values "
                                                   u"(font 1: lookups 1 and 2)")

    def test_sources_folders_and_other_files_are_not_checked(self):
        for name, data in (("Font.vfc", b"\x00\x01\x02\x03vfc"), ("Font.vfj", b'{"font": {}}'), ("Open.ttf", b"font"),
                           ("Tiny.ttf", b"\x00")):
            scan = fl.scan_font_file(self.write(name, data))
            self.assertEqual((scan["format"], scan["checked"], scan["count"]), (None, False, 0), name)
            self.assertNotIn("why_not", scan)
        folder = os.path.join(self.tmp, "Font.ufo")
        os.mkdir(folder)
        self.assertEqual(fl.scan_font_file(folder)["format"], None)
        self.assertEqual(fl.scan_font_file(os.path.join(self.tmp, "missing.ttf"))["format"], None)

    def test_a_damaged_file_says_why_it_was_not_checked(self):
        whole = fb.sfnt(font(crasher_gpos()))
        scan = fl.scan_font_file(self.write("Cut.ttf", whole[:60]))
        self.assertEqual((scan["format"], scan["checked"]), ("sfnt", False))
        self.assertIn("could not be read", scan["why_not"])
        bad = bytearray(whole)
        gpos_at = fb.tables_of(whole)[b"GPOS"][0]
        struct.pack_into(">H", bad, gpos_at + 8, 0xFFF0)  # (the LookupList past the table's end)
        scan = fl.scan_font_file(self.write("Bad.ttf", bytes(bad)))
        self.assertEqual(scan["checked"], False)
        self.assertIn("past the end", scan["why_not"])

    def test_a_crasher_found_before_a_damaged_part_still_counts(self):
        # (Codex, 2026-11-19: an error part-way threw away what was found, and the file was opened)
        broken = bytearray(fb.lookup(1, [fb.single(1, 0)]))
        struct.pack_into(">H", broken, 6, 0xFFF0)  # (its subtable past the table's end)
        gpos = fb.gpos([fb.lookup(1, [EMPTY]), bytes(broken), fb.lookup(1, [EMPTY])])
        scan = fl.scan_font_file(self.write("Half.ttf", fb.sfnt(font(gpos))))
        self.assertEqual((scan["checked"], scan["count"]), (False, 2), "the lookups after the damaged one are read")
        self.assertEqual([s["lookup"] for s in scan["fonts"][0]["subtables"]], [0, 2])
        self.assertIn("past the end", scan["why_not"])
        data = bytearray(fb.collection([font(crasher_gpos()), font(clean_gpos())]))
        second = struct.unpack_from(">I", data, 16)[0]
        struct.pack_into(">H", data, second + 4, 0xFFFF)  # (font 1's table directory past the file's end)
        scan = fl.scan_font_file(self.write("Half.ttc", bytes(data)))
        self.assertEqual((scan["checked"], scan["count"], [e["font"] for e in scan["fonts"]]), (False, 2, [0]))
        self.assertIn("could not be read", scan["why_not"])
        fixed, changed = fl.fixed_font_bytes(bytearray(data), "collection")
        self.assertEqual(changed, 2, "font 0 fixed; font 1 left as it is")

    def test_a_gpos_that_lists_its_data_over_and_over_is_read_only_so_far(self):
        # (Codex, 2026-11-19: lookups sharing one lookup of 65,535 subtables made 4 billion reads of 260 KB)
        count, lookups = 4000, 100
        sub = fb.single(1, 0)
        shared = struct.pack(">HHH", 1, 0, count) + struct.pack(">H", 6 + 2 * count) * count + sub
        first = fb.lookup(1, [EMPTY])
        listing = 2 + 2 * (lookups + 1)
        table = (struct.pack(">HHHHH", 1, 0, 10, 12, 14) + struct.pack(">HH", 0, 0) + struct.pack(">H", lookups + 1) +
                 struct.pack(">H", listing) + struct.pack(">H", listing + len(first)) * lookups + first + shared)
        self.assertLess(len(table), 20000)
        started = time.time()
        scan = fl.scan_font_file(self.write("Loop.ttf", fb.sfnt(font(table))))
        self.assertLess(time.time() - started, 10)
        self.assertEqual((scan["checked"], scan["count"]), (False, 1), "the crasher before the limit is kept")
        self.assertIn("over and over", scan["why_not"])
        with self.assertRaises(fl.FontFileLimit):
            fl.empty_single_adjustments(table)
        under = fb.gpos([fb.lookup(1, [EMPTY] + [sub] * 3000)])
        self.assertEqual(len(fl.empty_single_adjustments(under)), 1, "3,001 subtables: far under the limit")

    def test_lengths_past_the_file_are_refused_before_they_are_read(self):
        # (Codex, 2026-11-19: a collection of 4 billion fonts asked for 17 GB; a WOFF table could inflate past its length)
        scan = fl.scan_font_file(self.write("Huge.ttc", b"ttcf" + struct.pack(">HHI", 1, 0, 0xFFFFFFFF)))
        self.assertEqual((scan["format"], scan["checked"]), ("collection", False))
        self.assertIn("ends before", scan["why_not"])
        data = bytearray(fb.woff(font(crasher_gpos() + b"\0" * 5000)))
        self.assertEqual(bytes(data[44:48]), b"GPOS")
        struct.pack_into(">I", data, 44 + 12, 200)  # (its length said to be 200 bytes: the stream holds more)
        scan = fl.scan_font_file(self.write("Bomb.woff", bytes(data)))
        self.assertIn("longer than its directory says", scan["why_not"])
        struct.pack_into(">I", data, 44 + 12, fl.MAX_WOFF_TABLE + 1)
        self.assertIn("more than any font's", fl.scan_font_file(self.write("Bomb2.woff", bytes(data)))["why_not"])

    def test_a_copy_with_its_head_past_the_end_and_a_failed_write(self):
        data = bytearray(fb.sfnt(font(crasher_gpos())))
        entry = [12 + 16 * k for k in range(3) if bytes(data[12 + 16 * k:16 + 16 * k]) == b"head"][0]
        struct.pack_into(">I", data, entry + 8, len(data) + 100)  # (head past the file's end)
        path = self.write("NoHead.ttf", bytes(data))
        scan = fl.scan_font_file(path)
        self.assertEqual(scan["count"], 2, "head is not read by the check")
        target = os.path.join(self.tmp, "copies", "NoHead.ttf")
        self.assertEqual(fl.write_safe_copy(path, scan, target), 2, "fixed, head left as it is")
        real = os.rename

        def refused(*args):
            raise OSError("refused")
        fl.os.rename = refused
        try:
            self.assertRaises(OSError, fl.write_safe_copy, path, scan, os.path.join(self.tmp, "other", "NoHead.ttf"))
        finally:
            fl.os.rename = real
        self.assertEqual(os.listdir(os.path.join(self.tmp, "other")), [], "no .tmp left")


def filtered_lookup(kind, subtables, mark_set):
    """A lookup with flag 0x10 (UseMarkFilteringSet): its mark filtering set follows the subtable offsets."""
    out = struct.pack(">HHH", kind, 0x10, len(subtables))
    at = 6 + 2 * len(subtables) + 2
    offsets, body = b"", b""
    for sub in subtables:
        offsets += struct.pack(">H", at + len(body))
        body += sub
    return out + offsets + struct.pack(">H", mark_set) + body


NO_GLYPH_2 = fb.single(2, 0, 0, glyphs=())  # format 2 with no values over no glyph
NO_GLYPH_1 = fb.single(1, 4, glyphs=())  # format 1 over no glyph (the fixed copy of a static instance, 2026-11-27)


class NoGlyphTests(Case):
    """A single adjustment over no glyph crashed FontLab 7.2 as format 1 too (2026-11-27: NotoSansJP[wght] at wght
    700, its copy fixed to format 1, crashed in the same function): it is found in either format, and the copy
    takes it out of its lookup."""

    def gpos(self):
        return fb.gpos([fb.lookup(1, [fb.single(1, 4), NO_GLYPH_2, fb.single(2, 4, 3), NO_GLYPH_1]),
                        filtered_lookup(1, [NO_GLYPH_1, fb.single(1, 0), EMPTY], 7),
                        fb.lookup(9, [fb.extension(NO_GLYPH_2)])])

    def test_found_in_either_format(self):
        found = fl.empty_single_adjustments(self.gpos())
        self.assertEqual([(f["lookup"], f["subtable"], f["format"], f["glyphs"]) for f in found],
                         [(0, 1, 2, 0), (0, 3, 1, 0), (1, 0, 1, 0), (1, 2, 2, 3), (2, 0, 2, 0)])
        self.assertEqual(fl.empty_single_adjustments(fb.gpos([fb.lookup(1, [fb.single(1, 0)])])), [],
                         "format 1 over glyphs holds its value")

    def test_the_copy_takes_them_out(self):
        path = self.write("Static.ttf", fb.sfnt(font(self.gpos())))
        scan = fl.scan_font_file(path)
        self.assertEqual(scan["count"], 5)
        self.assertNotIn("lookup_at", fl.crasher_report(scan)[0]["subtables"][0], "offsets stay inside")
        target = os.path.join(self.tmp, "copy.ttf")
        self.assertEqual(fl.write_safe_copy(path, scan, target), 5)
        with open(target, "rb") as handle:
            copy = handle.read()
        self.assertEqual(fl.scan_font_file(target)["count"], 0)
        gpos_at, gpos_length = fb.tables_of(copy)[b"GPOS"][:2]
        gpos = copy[gpos_at:gpos_at + gpos_length]
        listing = struct.unpack_from(">H", gpos, 8)[0]

        def subtables(li):
            lookup = listing + struct.unpack_from(">H", gpos, listing + 2 + 2 * li)[0]
            kind, flag, count = struct.unpack_from(">HHH", gpos, lookup)
            offsets = struct.unpack_from(">%dH" % count, gpos, lookup + 6)
            forms = [struct.unpack_from(">H", gpos, lookup + o)[0] for o in offsets]
            mark_set = struct.unpack_from(">H", gpos, lookup + 6 + 2 * count)[0] if flag & 0x10 else None
            return forms, mark_set
        self.assertEqual(subtables(0), ([1, 2], None), "the two over glyphs kept as they were")
        self.assertEqual(subtables(1), ([1, 1], 7), "the mark filtering set moved up with the offsets")
        self.assertEqual(subtables(2), ([], None), "a lookup of one such subtable is left with none")
        self.assertEqual(len(copy), os.path.getsize(path), "nothing moved")


class CopyTests(Case):
    def copy(self, name, data):
        path = self.write(name, data)
        scan = fl.scan_font_file(path)
        target = fl.safe_copy_path(path, scan, os.path.join(self.tmp, "copies"))
        with open(path, "rb") as handle:
            before = hashlib.sha256(handle.read()).hexdigest()
        changed = fl.write_safe_copy(path, scan, target)
        with open(path, "rb") as handle:
            self.assertEqual(hashlib.sha256(handle.read()).hexdigest(), before, "the original is only read")
        with open(target, "rb") as handle:
            return path, scan, target, changed, handle.read()

    def assert_whole(self, data, base=0, alone=True):
        """Every table's checksum right, and (for a file of one font) the checkSumAdjustment."""
        tables = fb.tables_of(data, base)
        head_at = tables[b"head"][0]
        zeroed = bytearray(data)
        struct.pack_into(">I", zeroed, head_at + 8, 0)
        for tag, (offset, length, check) in tables.items():
            self.assertEqual(fb.checksum(zeroed[offset:offset + length]), check, tag)
        if alone:
            adjustment = struct.unpack_from(">I", data, head_at + 8)[0]
            self.assertEqual((0xB1B0AFBA - fb.checksum(zeroed)) & 0xFFFFFFFF, adjustment)

    def assert_only_the_subtables_changed(self, original, copy, scan, base=0):
        """Every table the same but GPOS, where only the bytes of the subtables found changed, and head's
        checkSumAdjustment; returns how many GPOS bytes changed."""
        old, new = fb.tables_of(original, base), fb.tables_of(copy, base)
        self.assertEqual(sorted(old), sorted(new))
        changed = []
        for tag in old:
            a = original[old[tag][0]:old[tag][0] + old[tag][1]]
            b = copy[new[tag][0]:new[tag][0] + new[tag][1]]
            if tag == b"head":
                self.assertEqual((a[:8], a[12:]), (b[:8], b[12:]), "head: only checkSumAdjustment")
            elif tag == b"GPOS":
                changed = [k for k in range(len(a)) if a[k:k + 1] != b[k:k + 1]]
                spots = set()
                for entry in scan["fonts"]:
                    for s in entry["subtables"]:
                        spots.update([s["at"] - old[tag][0] + 1, s["at"] - old[tag][0] + 5])
                self.assertTrue(set(changed) <= spots, (changed, spots))
            else:
                self.assertEqual(a, b, tag)
        return len(changed)

    def test_an_sfnt_copy_holds_format_1_with_no_values_and_nothing_else_changed(self):
        original = fb.sfnt(font(crasher_gpos()))
        path, scan, target, changed, copy = self.copy("Crash[wght].ttf", original)
        self.assertEqual(changed, 2)
        self.assertEqual(os.path.basename(target), "Crash[wght].ttf", "named after the original")
        self.assertEqual(os.path.dirname(os.path.dirname(target)), os.path.join(self.tmp, "copies"))
        self.assertEqual(len(copy), len(original), "nothing moved")
        again = fl.scan_font_file(target)
        self.assertEqual((again["format"], again["checked"], again["count"]), ("sfnt", True, 0))
        gpos_at, gpos_length = fb.tables_of(copy)[b"GPOS"][:2]
        gpos = copy[gpos_at:gpos_at + gpos_length]
        for entry in scan["fonts"]:
            for s in entry["subtables"]:
                at = s["at"] - gpos_at
                self.assertEqual(struct.unpack_from(">HHH", gpos, at)[0::2], (1, 0), "format 1, ValueFormat 0")
                self.assertEqual(struct.unpack_from(">H", gpos, at + 2), struct.unpack_from(">H", original, s["at"] + 2),
                                 "the same coverage")
        self.assert_whole(copy)
        self.assertEqual(self.assert_only_the_subtables_changed(original, copy, scan), 3,
                         "both formats (2 to 1) and one ValueFormat (4 to 0)")
        self.assertEqual(fl.empty_single_adjustments(gpos), [])

    def test_a_shared_subtable_is_changed_once(self):
        # Two lookups whose subtables are one: the LookupList at 14 (2 lookups, at 6 and 14 from it), each lookup
        # pointing at the subtable 22 bytes into the LookupList
        listing = struct.pack(">HHH", 2, 6, 14) + struct.pack(">HHHH", 1, 0, 1, 16) + struct.pack(">HHHH", 1, 0, 1, 8)
        gpos = struct.pack(">HHHHH", 1, 0, 10, 12, 14) + struct.pack(">HH", 0, 0) + listing + EMPTY
        path, scan, target, changed, copy = self.copy("Shared.otf", fb.sfnt(font(gpos), b"OTTO"))
        self.assertEqual([s["lookup"] for s in scan["fonts"][0]["subtables"]], [0, 1])
        self.assertEqual((scan["count"], changed), (2, 1), "one subtable, changed once")
        self.assertEqual(fl.scan_font_file(target)["count"], 0)
        self.assert_whole(copy)

    def test_a_collection_copy(self):
        fonts = [font(clean_gpos()), font(crasher_gpos())]
        original = fb.collection(fonts)
        path, scan, target, changed, copy = self.copy("Family.ttc", original)
        self.assertEqual((changed, len(copy)), (2, len(original)))
        self.assertEqual(fl.scan_font_file(target)["count"], 0)
        for k, bytes_changed in ((0, 0), (1, 3)):
            base = struct.unpack_from(">I", copy, 12 + 4 * k)[0]
            self.assert_whole(copy, base, alone=False)
            self.assertEqual(self.assert_only_the_subtables_changed(original, copy, scan, base), bytes_changed)

    def test_a_woff_copy_is_its_sfnt(self):
        for version, extension in ((b"\x00\x01\x00\x00", ".ttf"), (b"OTTO", ".otf")):
            tables = font(crasher_gpos())
            path, scan, target, changed, copy = self.copy("Web.woff", fb.woff(tables, version))
            self.assertEqual((changed, os.path.basename(target)), (2, "Web" + extension))
            self.assertEqual(copy[:4], version)
            again = fl.scan_font_file(target)
            self.assertEqual((again["format"], again["checked"], again["count"]), ("sfnt", True, 0))
            self.assert_whole(copy)
            new = fb.tables_of(copy)
            self.assertEqual(sorted(new), sorted(tables))
            offset, length = new[b"name"][:2]
            self.assertEqual(copy[offset:offset + length], tables[b"name"])

    def test_a_woff2_is_not_copied(self):
        fl._brotli_decompress = lambda: (lambda data: data)
        path = self.write("Web.woff2", fb.woff2(font(crasher_gpos())))
        scan = fl.scan_font_file(path)
        self.assertRaises(fl.FontFileError, fl.safe_copy_path, path, scan, self.tmp)
        self.assertRaises(fl.FontFileError, fl.write_safe_copy, path, scan, os.path.join(self.tmp, "x.ttf"))

    def test_two_originals_of_one_name_get_two_copies(self):
        os.mkdir(os.path.join(self.tmp, "a"))
        os.mkdir(os.path.join(self.tmp, "b"))
        data = fb.sfnt(font(crasher_gpos()))
        one, two = self.write(os.path.join("a", "Same.ttf"), data), self.write(os.path.join("b", "Same.ttf"), data)
        scan = fl.scan_font_file(one)
        self.assertNotEqual(fl.safe_copy_path(one, scan, self.tmp), fl.safe_copy_path(two, scan, self.tmp))
        self.assertEqual(fl.safe_copy_path(one, scan, self.tmp), fl.safe_copy_path(one, scan, self.tmp))


if __name__ == "__main__":
    unittest.main()
