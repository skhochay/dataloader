"""File parsers. Each yields (row_number, dict) and raises ParseError on bad input.

Parsers are generators, so large files stream instead of loading into memory
(the exceptions are noted per format). Add a format by writing a function and
registering its extension in PARSERS at the bottom.

How each format becomes rows
  .csv .tsv  first line is the header; one row per following line
  .json      an array of objects (or one object); one row per object
  .txt       one row per non-empty line:  {"text": "<the line>"}
  .xml       one row per child element of the root element
  .xlsx .xls first sheet; first non-empty row is the header; one row per later row
  .avro      one row per record
  .parquet   one row per record
"""
import base64
import csv
import importlib
import json
import math
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import partial
from pathlib import Path


class ParseError(Exception):
    """The file's contents are not loadable."""


# ---- helpers ---------------------------------------------------------------
def _require(module, pip_name, fmt):
    try:
        return importlib.import_module(module)
    except ImportError:
        raise ParseError(
            f"reading {fmt} files needs the '{pip_name}' package. Install it with: pip install {pip_name}"
        ) from None


def _jsonable(value):
    """Make a value safe for the database's JSON column."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None  # NaN/Infinity are not valid JSON
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return str(value)


def _blank(value):
    return value is None or (isinstance(value, str) and not value.strip())


def _table_rows(rows):
    """Spreadsheet rows (lists of cell values) -> (row_number, dict) using the first non-empty row as header."""
    headers = None
    number = 0
    for raw in rows:
        values = list(raw)
        if all(_blank(v) for v in values):
            continue
        if headers is None:
            headers = ["" if v is None else str(v).strip() for v in values]
            while headers and not headers[-1]:
                headers.pop()  # sheets are often wider than their data
            for position, name in enumerate(headers, start=1):
                if not name:
                    raise ParseError(f"header row: column {position} has no name")
            if len(set(headers)) != len(headers):
                duplicate = next(h for h in headers if headers.count(h) > 1)
                raise ParseError(f"header row: column name '{duplicate}' appears more than once")
            continue
        number += 1
        if any(not _blank(v) for v in values[len(headers):]):
            raise ParseError(f"row {number}: more values than header columns")
        values = (values + [None] * len(headers))[: len(headers)]
        yield number, {name: _jsonable(v) for name, v in zip(headers, values)}
    if headers is None:
        raise ParseError("the sheet is empty")


# ---- text formats ----------------------------------------------------------
def parse_csv(path, delimiter=","):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh, delimiter=delimiter)
        if not reader.fieldnames:
            raise ParseError("file has no header row")
        for number, row in enumerate(reader, start=1):
            if None in row:
                raise ParseError(f"row {number}: more values than header columns")
            if any(value is None for value in row.values()):
                raise ParseError(f"row {number}: fewer values than header columns")
            yield number, row


def parse_json(path):
    with open(path, encoding="utf-8-sig") as fh:
        try:
            payload = json.load(fh)
        except json.JSONDecodeError as exc:
            raise ParseError(f"invalid JSON: {exc}") from exc
    if isinstance(payload, dict):
        payload = [payload]
    if not isinstance(payload, list):
        raise ParseError("expected a JSON object or an array of objects")
    for number, item in enumerate(payload, start=1):
        if not isinstance(item, dict):
            raise ParseError(f"row {number}: expected an object")
        yield number, item


def parse_txt(path):
    with open(path, encoding="utf-8-sig") as fh:
        number = 0
        for line in fh:
            text = line.rstrip("\r\n")
            if not text.strip():
                continue
            number += 1
            yield number, {"text": text}


def _local(tag):
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else str(tag)


def _xml_value(element):
    """Element -> string (leaf) or dict (attributes as '@name', child tags, text as '#text')."""
    children = list(element)
    text = (element.text or "").strip()
    if not children and not element.attrib:
        return text
    out = {f"@{_local(k)}": v for k, v in element.attrib.items()}
    grouped = {}
    for child in children:
        grouped.setdefault(_local(child.tag), []).append(_xml_value(child))
    for key, values in grouped.items():
        out[key] = values[0] if len(values) == 1 else values  # repeated tags become a list
    if text:
        out["#text"] = text
    return out


def parse_xml(path):
    """Each child of the root element is one row. The whole file is read into memory."""
    try:
        from defusedxml import DefusedXmlException
        from defusedxml import ElementTree as ET
    except ImportError:
        raise ParseError("reading .xml files needs the 'defusedxml' package. Install it with: pip install defusedxml") from None
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        raise ParseError(f"invalid XML: {exc}") from exc
    except DefusedXmlException as exc:
        raise ParseError("XML entities and DTDs are not allowed") from exc
    children = list(root)
    if not children:
        raise ParseError("the root element has no child elements to load as rows")
    for number, child in enumerate(children, start=1):
        value = _xml_value(child)
        yield number, value if isinstance(value, dict) else {_local(child.tag): value}


# ---- spreadsheets ----------------------------------------------------------
def parse_xlsx(path):
    """First worksheet. Formula cells give the value Excel last saved with the file."""
    openpyxl = _require("openpyxl", "openpyxl", ".xlsx")
    try:
        book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises several unrelated types for bad files
        raise ParseError(f"invalid .xlsx file: {exc}") from exc
    try:
        if not book.worksheets:
            raise ParseError("the workbook has no sheets")
        yield from _table_rows(book.worksheets[0].iter_rows(values_only=True))
    finally:
        book.close()  # release the file so it can be moved afterwards


def _xls_value(xlrd, book, cell):
    kind = cell.ctype
    if kind in (xlrd.XL_CELL_EMPTY, xlrd.XL_CELL_BLANK):
        return None
    if kind == xlrd.XL_CELL_DATE:
        try:
            return xlrd.xldate_as_datetime(cell.value, book.datemode).isoformat()
        except xlrd.XLDateError:
            return cell.value
    if kind == xlrd.XL_CELL_BOOLEAN:
        return bool(cell.value)
    if kind == xlrd.XL_CELL_ERROR:
        return xlrd.error_text_from_code.get(cell.value, "#ERROR")
    if kind == xlrd.XL_CELL_NUMBER and float(cell.value).is_integer() and abs(cell.value) < 1e15:
        return int(cell.value)  # Excel stores whole numbers as floats
    return cell.value


def parse_xls(path):
    """First worksheet of a legacy Excel file. The whole file is read into memory."""
    xlrd = _require("xlrd", "xlrd", ".xls")
    try:
        book = xlrd.open_workbook(file_contents=Path(path).read_bytes())
    except xlrd.XLRDError as exc:
        raise ParseError(f"invalid .xls file: {exc}") from exc
    if book.nsheets == 0:
        raise ParseError("the workbook has no sheets")
    sheet = book.sheet_by_index(0)
    yield from _table_rows(
        [_xls_value(xlrd, book, sheet.cell(r, c)) for c in range(sheet.ncols)] for r in range(sheet.nrows)
    )


# ---- binary columnar / record formats --------------------------------------
def parse_avro(path):
    fastavro = _require("fastavro", "fastavro", ".avro")
    with open(path, "rb") as fh:
        try:
            for number, record in enumerate(fastavro.reader(fh), start=1):
                if not isinstance(record, dict):
                    raise ParseError(f"row {number}: expected a record, got {type(record).__name__}")
                yield number, _jsonable(record)
        except ParseError:
            raise
        except (ValueError, EOFError) as exc:
            raise ParseError(f"invalid Avro file: {exc}") from exc


def parse_parquet(path, batch_rows=5000):
    pa = _require("pyarrow", "pyarrow", ".parquet")
    import pyarrow.parquet as pq

    with open(path, "rb") as fh:
        try:
            number = 0
            for batch in pq.ParquetFile(fh).iter_batches(batch_size=batch_rows):
                for record in batch.to_pylist():
                    number += 1
                    yield number, _jsonable(record)
        except pa.ArrowException as exc:
            raise ParseError(f"invalid Parquet file: {exc}") from exc


PARSERS = {
    ".csv": parse_csv,
    ".json": parse_json,
    ".txt": parse_txt,
    ".avro": parse_avro,
    ".parquet": parse_parquet,
    ".xml": parse_xml,
    ".tsv": partial(parse_csv, delimiter="\t"),
    ".xlsx": parse_xlsx,
    ".xls": parse_xls,
}
