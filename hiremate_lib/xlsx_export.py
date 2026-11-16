"""纯 Python 标准库生成标准 Office Open XML (.xlsx) 工作簿（零第三方依赖、含防公式注入与表头冻结）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence
from xml.sax.saxutils import escape
import zipfile

from .normalize import sanitize_csv_cell

_INVALID_XML_CHARS = frozenset(
    chr(c) for c in range(0x00, 0x20) if c not in (0x09, 0x0A, 0x0D)
)


def _xml_safe(value: object) -> str:
    safe_cell = sanitize_csv_cell(value)
    cleaned = "".join(ch for ch in safe_cell if ch not in _INVALID_XML_CHARS)
    return escape(cleaned, {'"': "&quot;", "'": "&apos;"})


def _col_letter(col_idx_1based: int) -> str:
    result: list[str] = []
    n = col_idx_1based
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result.append(chr(ord("A") + rem))
    return "".join(reversed(result))


def write_xlsx_workbook(
    out_path: Path,
    *,
    sheet_name: str,
    headers: Sequence[str],
    rows: Sequence[Sequence[object]],
) -> dict[str, Any]:
    out_path = out_path.expanduser().resolve()
    if out_path.suffix.lower() != ".xlsx":
        out_path = out_path.with_suffix(".xlsx")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    col_widths: list[float] = []
    for col_i, h in enumerate(headers):
        max_len = len(str(h)) * 2
        for row in rows[:200]:
            if col_i < len(row) and row[col_i] is not None:
                text = str(row[col_i])
                cjk_len = sum(2 if ord(ch) > 127 else 1 for ch in text)
                max_len = max(max_len, min(48, cjk_len))
        col_widths.append(max(11.0, min(46.0, max_len + 3.0)))

    cols_xml = "".join(
        f'<col min="{idx}" max="{idx}" width="{w:.1f}" customWidth="1"/>'
        for idx, w in enumerate(col_widths, start=1)
    )

    row_xml_parts: list[str] = []
    # Header row (style s="1")
    header_cells = "".join(
        f'<c r="{_col_letter(c_idx)}1" s="1" t="inlineStr"><is><t xml:space="preserve">{_xml_safe(h)}</t></is></c>'
        for c_idx, h in enumerate(headers, start=1)
    )
    row_xml_parts.append(f'<row r="1" ht="24" customHeight="1">{header_cells}</row>')

    # Data rows (style s="2")
    for r_idx, row in enumerate(rows, start=2):
        cells = "".join(
            f'<c r="{_col_letter(c_idx)}{r_idx}" s="2" t="inlineStr"><is><t xml:space="preserve">{_xml_safe(val)}</t></is></c>'
            for c_idx, val in enumerate(row, start=1)
        )
        row_xml_parts.append(f'<row r="{r_idx}">{cells}</row>')

    last_col = _col_letter(max(1, len(headers)))
    last_row = len(rows) + 1

    sheet_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f'<dimension ref="A1:{last_col}{last_row}"/>'
        '<sheetViews><sheetView tabSelected="1" workbookViewId="0">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '</sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="19"/>'
        f'<cols>{cols_xml}</cols>'
        f'<sheetData>{"".join(row_xml_parts)}</sheetData>'
        f'<autoFilter ref="A1:{last_col}{last_row}"/>'
        '</worksheet>'
    )

    styles_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2">'
        '<font><sz val="10.5"/><color rgb="FF1E293B"/><name val="PingFang SC"/></font>'
        '<font><b/><sz val="10.5"/><color rgb="FFFFFFFF"/><name val="PingFang SC"/></font>'
        '</fonts>'
        '<fills count="3">'
        '<fill><patternFill patternType="none"/></fill>'
        '<fill><patternFill patternType="gray125"/></fill>'
        '<fill><patternFill patternType="solid"><fgColor rgb="FF1D4ED8"/><bgColor indexed="64"/></patternFill></fill>'
        '</fills>'
        '<borders count="2">'
        '<border><left/><right/><top/><bottom/><diagonal/></border>'
        '<border>'
        '<left style="thin"><color rgb="FFE2E8F0"/></left>'
        '<right style="thin"><color rgb="FFE2E8F0"/></right>'
        '<top style="thin"><color rgb="FFE2E8F0"/></top>'
        '<bottom style="thin"><color rgb="FFE2E8F0"/></bottom>'
        '<diagonal/>'
        '</border>'
        '</borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="3">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1" applyAlignment="1">'
        '<alignment vertical="center"/>'
        '</xf>'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="1" xfId="0" applyFont="1" applyBorder="1" applyAlignment="1">'
        '<alignment vertical="center" wrapText="1"/>'
        '</xf>'
        '</cellXfs>'
        '</styleSheet>'
    )

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f'<sheets><sheet name="{_xml_safe(sheet_name)[:31]}" sheetId="1" r:id="rId1"/></sheets>'
        '</workbook>'
    )

    wb_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
        '</Relationships>'
    )

    root_rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )

    content_types_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        '</Types>'
    )

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml)
        zf.writestr("_rels/.rels", root_rels_xml)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", wb_rels_xml)
        zf.writestr("xl/styles.xml", styles_xml)
        zf.writestr("xl/worksheets/sheet1.xml", sheet_xml)

    return {
        "ok": True,
        "format": "xlsx",
        "out_path": str(out_path),
        "exported_count": len(rows),
    }
