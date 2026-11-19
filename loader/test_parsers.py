import importlib.util
import tempfile
import unittest
from pathlib import Path

from .parsers import PARSERS, ParseError


def have(module):
    return importlib.util.find_spec(module) is not None


class ParserTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name, text="", data=None):
        path = self.dir / name
        if data is not None:
            path.write_bytes(data)
        else:
            path.write_text(text, encoding="utf-8")
        return path

    def rows(self, path):
        return list(PARSERS[path.suffix](path))

    def test_supported_types_are_exactly_the_requested_ones(self):
        self.assertEqual(
            set(PARSERS), {".csv", ".json", ".txt", ".avro", ".parquet", ".xml", ".tsv", ".xlsx", ".xls"}
        )

    def test_tsv(self):
        self.assertEqual(self.rows(self.write("a.tsv", "a\tb\n1\t2\n")), [(1, {"a": "1", "b": "2"})])

    def test_txt_one_row_per_non_empty_line(self):
        rows = self.rows(self.write("n.txt", "one\r\n\r\ntwo\n"))
        self.assertEqual(rows, [(1, {"text": "one"}), (2, {"text": "two"})])

    def test_xml_children_become_rows(self):
        xml = '<orders><order id="1"><c>A</c><tag>x</tag><tag>y</tag></order><order id="2"><c>B</c></order></orders>'
        rows = self.rows(self.write("o.xml", xml))
        self.assertEqual(rows[0], (1, {"@id": "1", "c": "A", "tag": ["x", "y"]}))
        self.assertEqual(rows[1], (2, {"@id": "2", "c": "B"}))

    def test_xml_rejects_malformed_and_entities(self):
        with self.assertRaises(ParseError):
            self.rows(self.write("bad.xml", "<a><b></a>"))
        bomb = '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x><y>&a;</y></x>'
        with self.assertRaises(ParseError):
            self.rows(self.write("bomb.xml", bomb))

    def test_xml_without_children_is_rejected(self):
        with self.assertRaises(ParseError):
            self.rows(self.write("empty.xml", "<orders/>"))

    @unittest.skipUnless(have("openpyxl"), "openpyxl not installed")
    def test_xlsx(self):
        import datetime

        import openpyxl

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append([None, None])  # leading blank row is skipped
        ws.append(["id", "name", "when", None])  # trailing blank header is ignored
        ws.append([1, "Ann", datetime.datetime(2026, 1, 2, 3, 4, 5)])
        ws.append([None, None, None])  # blank row is skipped
        ws.append([2, "Bob", None])
        path = self.dir / "t.xlsx"
        wb.save(path)
        self.assertEqual(
            self.rows(path),
            [
                (1, {"id": 1, "name": "Ann", "when": "2026-01-02T03:04:05"}),
                (2, {"id": 2, "name": "Bob", "when": None}),
            ],
        )

    @unittest.skipUnless(have("openpyxl"), "openpyxl not installed")
    def test_xlsx_duplicate_headers_and_corrupt_file(self):
        import openpyxl

        wb = openpyxl.Workbook()
        wb.active.append(["a", "a"])
        wb.active.append([1, 2])
        path = self.dir / "dup.xlsx"
        wb.save(path)
        with self.assertRaises(ParseError):
            self.rows(path)
        with self.assertRaises(ParseError):
            self.rows(self.write("junk.xlsx", data=b"not a zip"))

    @unittest.skipUnless(have("fastavro"), "fastavro not installed")
    def test_avro(self):
        import fastavro

        schema = {"type": "record", "name": "R", "fields": [{"name": "id", "type": "int"}, {"name": "n", "type": "string"}]}
        path = self.dir / "t.avro"
        with open(path, "wb") as fh:
            fastavro.writer(fh, schema, [{"id": 1, "n": "a"}, {"id": 2, "n": "b"}])
        self.assertEqual(self.rows(path), [(1, {"id": 1, "n": "a"}), (2, {"id": 2, "n": "b"})])
        with self.assertRaises(ParseError):
            self.rows(self.write("junk.avro", data=b"nope"))

    @unittest.skipUnless(have("pyarrow"), "pyarrow not installed")
    def test_parquet(self):
        import pyarrow as pa
        import pyarrow.parquet as pq

        path = self.dir / "t.parquet"
        pq.write_table(pa.table({"id": [1, 2], "x": [1.5, float("nan")]}), path)
        self.assertEqual(self.rows(path), [(1, {"id": 1, "x": 1.5}), (2, {"id": 2, "x": None})])
        with self.assertRaises(ParseError):
            self.rows(self.write("junk.parquet", data=b"nope"))

    @unittest.skipUnless(have("xlrd"), "xlrd not installed")
    def test_xls_rejects_garbage(self):
        with self.assertRaises(ParseError):
            self.rows(self.write("junk.xls", data=b"not an excel file"))

    def test_missing_library_gives_an_install_hint(self):
        from . import parsers

        real = parsers.importlib.import_module

        def fake(name, *a, **k):
            raise ImportError(name)

        parsers.importlib.import_module = fake
        try:
            with self.assertRaises(ParseError) as ctx:
                list(parsers.parse_avro(self.write("x.avro", data=b"")))
        finally:
            parsers.importlib.import_module = real
        self.assertIn("pip install fastavro", str(ctx.exception))
