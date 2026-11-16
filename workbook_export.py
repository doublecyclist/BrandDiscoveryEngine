"""Populate the bundled Excel template without extra office software.

The template is authored with Artifact Tool. Only cell data and bounded filter
ranges are replaced here; formatting, panes, and workbook metadata are retained.
"""
import copy
import io
from pathlib import Path
import re
import zipfile
from xml.etree import ElementTree as ET

NS = 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'
ET.register_namespace('', NS)


def tag(name):
    return '{' + NS + '}' + name


def column(index):
    output = ''
    while index:
        index, remainder = divmod(index - 1, 26)
        output = chr(65 + remainder) + output
    return output


def cell(address, value, style=None):
    attrs = {'r': address}
    if style is not None:
        attrs['s'] = style
    node = ET.Element(tag('c'), attrs)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ET.SubElement(node, tag('v')).text = str(value)
    elif value is not None:
        node.set('t', 'inlineStr')
        text = ET.SubElement(ET.SubElement(node, tag('is')), tag('t'))
        text.set('{http://www.w3.org/XML/1998/namespace}space', 'preserve')
        text.text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value))
    return node


def excel_bytes(sheets, template=None):
    template = template or Path(__file__).resolve().parent / 'excel_template.xlsx'
    output = io.BytesIO()
    with zipfile.ZipFile(template) as source, zipfile.ZipFile(output, 'w', zipfile.ZIP_DEFLATED) as dest:
        updates = {}
        for i, sheet in enumerate(sheets, 1):
            path = f'xl/worksheets/sheet{i}.xml'
            xml = ET.fromstring(source.read(path))
            data = xml.find(tag('sheetData'))
            header = next((r for r in data if r.get('r') == '4'), None)
            body = next((r for r in data if r.get('r') == '5'), None)
            header_styles = {c.get('r').rstrip('0123456789'): c.get('s') for c in header} if header is not None else {}
            body_styles = {c.get('r').rstrip('0123456789'): c.get('s') for c in body} if body is not None else {}
            for row in list(data):
                if int(row.get('r', 0)) >= 4:
                    data.remove(row)
            h = ET.SubElement(data, tag('row'), {'r': '4', 'ht': '34', 'customHeight': '1'})
            for c, label in enumerate(sheet['columns'], 1):
                h.append(cell(column(c) + '4', label, header_styles.get(column(c), header_styles.get('A'))))
            for r, values in enumerate(sheet['rows'], 5):
                height = '70' if sheet['name'] in ('Evidence', 'Method') else '54'
                row = ET.SubElement(data, tag('row'), {'r': str(r), 'ht': height, 'customHeight': '1'})
                for c, value in enumerate(values, 1):
                    row.append(cell(column(c) + str(r), value, body_styles.get(column(c), body_styles.get('A'))))
            cols = xml.find(tag('cols'))
            if cols is None:
                cols = ET.Element(tag('cols'))
                xml.insert(list(xml).index(data), cols)
            cols.clear()
            for c, label in enumerate(sheet['columns'], 1):
                width = 12 if label == 'Rank' else 18 if any(x in label.lower() for x in ('score', 'points', 'leads')) else 30
                if any(x in label.lower() for x in ('reason', 'evidence', 'description', 'details', 'competing', 'incomplete', 'action', 'links', 'url', 'message')):
                    width = 65 if sheet['name'] != 'Method' else 120
                ET.SubElement(cols, tag('col'), {'min': str(c), 'max': str(c), 'width': str(width), 'customWidth': '1'})
            end = max(4, len(sheet['rows']) + 4)
            ref = f'A4:{column(len(sheet["columns"]))}{end}'
            dim = xml.find(tag('dimension'))
            if dim is not None:
                dim.set('ref', 'A1:' + ref.split(':')[1])
            filt = xml.find(tag('autoFilter'))
            if filt is None:
                filt = ET.Element(tag('autoFilter'))
                # autoFilter follows sheetData (and any sheetProtection).
                pos = list(xml).index(data) + 1
                xml.insert(pos, filt)
            filt.set('ref', ref)
            updates[path] = ET.tostring(xml, encoding='utf-8', xml_declaration=True)
        for item in source.infolist():
            dest.writestr(item, updates.get(item.filename, source.read(item.filename)))
    return output.getvalue()
