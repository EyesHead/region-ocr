"""Source-ordered DOCX HTML export, with optional recognition of image equations.

Word runs are inline formatting boundaries, not paragraph or word boundaries.
This reader deliberately leaves their whitespace untouched. It never edits the
Office source and does not write intermediate recognition/cache files.
"""

from __future__ import annotations

import base64
import html
import mimetypes
import posixpath
import re
import zipfile
from io import BytesIO
from pathlib import Path

from lxml import etree


def render_windows_metafile(data):
    """Render the full WMF/EMF with Windows System.Drawing, not Pillow's GDI.

    Pillow's handler clips some valid Office WMF window mappings. System.Drawing
    preserves them. Temporary source/PNG files are private and always removed.
    """
    import os
    import subprocess
    import shutil
    import tempfile
    from PIL import Image
    if os.name != 'nt':
        raise RuntimeError('WMF/EMF rendering currently requires Windows System.Drawing')
    executable = shutil.which('pwsh') or shutil.which('powershell')
    if not executable:
        builtin = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        executable = str(builtin) if builtin.is_file() else None
    if not executable:
        raise RuntimeError('WMF/EMF rendering requires Windows PowerShell or PowerShell 7')
    with tempfile.TemporaryDirectory(prefix='docling-metafile-') as folder:
        source = Path(folder) / 'asset.wmf'
        target = Path(folder) / 'asset.png'
        source.write_bytes(data)
        # Paths are generated locally; no source document text enters the command.
        script = r"""
Add-Type -AssemblyName System.Drawing
$im = $null; $bitmap = $null; $graphics = $null
try {
 $im = [System.Drawing.Image]::FromFile('__SOURCE__')
 $scale = [Math]::Max(1.0, 216.0 / [Math]::Max(1.0, $im.HorizontalResolution))
 $w = [Math]::Max(1, [int][Math]::Round($im.Width * $scale))
 $h = [Math]::Max(1, [int][Math]::Round($im.Height * $scale))
 $bitmap = [System.Drawing.Bitmap]::new($w, $h)
 $graphics = [System.Drawing.Graphics]::FromImage($bitmap)
 $graphics.Clear([System.Drawing.Color]::White)
 $graphics.DrawImage($im, 0, 0, $w, $h)
 $bitmap.Save('__TARGET__', [System.Drawing.Imaging.ImageFormat]::Png)
} finally {
 if ($graphics) { $graphics.Dispose() }
 if ($bitmap) { $bitmap.Dispose() }
 if ($im) { $im.Dispose() }
}
""".replace('__SOURCE__', str(source).replace("'", "''")).replace('__TARGET__', str(target).replace("'", "''"))
        encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        completed = subprocess.run([executable, '-NoProfile', '-NonInteractive',
                                    '-EncodedCommand', encoded], capture_output=True,
                                   timeout=30, creationflags=subprocess.CREATE_NO_WINDOW)
        if completed.returncode or not target.is_file():
            detail = completed.stderr.decode('utf-8', errors='replace').strip()
            raise RuntimeError('System.Drawing could not render the source metafile: '+detail)
        with Image.open(target) as image:
            return image.convert('RGB')


NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "v": "urn:schemas-microsoft-com:vml",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}


def _q(prefix, local):
    return "{" + NS[prefix] + "}" + local


def _local(node):
    return etree.QName(node).localname


def _val(node, default=None):
    return node.get(_q("w", "val"), default) if node is not None else default


def _on(node):
    return node is not None and _val(node, "1").lower() not in {"0", "false", "off"}


CSS = """
body { max-width: 1120px; margin: 2em auto; padding: 0 1em; line-height: 1.45;
       font-family: Arial, sans-serif; color: #111; }
p { margin: .55em 0; }
.table-scroll { max-width: 100%; overflow-x: auto; margin: 1em 0; }
table { border-collapse: collapse; width: 100%; }
td, th { border: 1px solid #777; padding: .4em .55em; vertical-align: top; }
td p, th p { margin: .2em 0; }
img { max-width: 100%; height: auto; vertical-align: middle; }
math { font-size: 1.05em; }
math[display=block] { margin: .6em 0; }
.office-tab { white-space: pre; }
.office-list-label { white-space: pre; }
.office-note { font-size: .9em; }
"""


class DocxHtmlExporter:
    def __init__(self, source, recognize_formula=None, diagnostic=None, *, source_name=None):
        self.source = None if hasattr(source, "read") else Path(source)
        self.source_name = str(source_name or (self.source if self.source is not None else getattr(source, "name", "Документ")))
        self.archive = zipfile.ZipFile(source)
        self.recognize_formula = recognize_formula
        self.diagnostic = diagnostic or (lambda *args, **kwargs: None)
        self.parser = etree.XMLParser(resolve_entities=False, no_network=True)
        self.relationships = self._relationships("word/document.xml")
        self.styles = {}
        self.numbers = {}
        self.number_values = {}
        self.image_index = 0
        self.stats = {"tables": 0, "images": 0, "image_math": 0, "native_math": 0,
                      "links_removed": 0, "source_breaks": 0, "paragraphs": 0}
        self._read_styles()
        self._read_numbering()
        self.notes = {"footnote": set(), "endnote": set()}

    def close(self):
        self.archive.close()

    def _read_xml(self, part):
        return etree.fromstring(self.archive.read(part), parser=self.parser)

    def _relationships(self, part):
        directory, name = posixpath.split(part)
        relpart = posixpath.join(directory, "_rels", name + ".rels")
        if relpart not in self.archive.namelist():
            return {}
        result = {}
        for rel in self._read_xml(relpart):
            if rel.get("TargetMode") == "External":
                continue
            target = rel.get("Target", "")
            target = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(directory, target))
            if target.startswith("../"):
                continue
            result[rel.get("Id")] = target
        return result

    def _read_styles(self):
        if "word/styles.xml" not in self.archive.namelist():
            return
        for style in self._read_xml("word/styles.xml").findall("w:style", NS):
            props = style.find("w:pPr", NS)
            outline = props.find("w:outlineLvl", NS) if props is not None else None
            name = _val(style.find("w:name", NS), "")
            level = None
            if outline is not None:
                try:
                    level = min(6, int(_val(outline)) + 1)
                except (ValueError, TypeError):
                    pass
            if level is None and (match := re.search(r"(?:heading|заголовок)\s*(\d)", name, re.I)):
                level = min(6, int(match[1]))
            self.styles[style.get(_q("w", "styleId"))] = level

    def _read_numbering(self):
        if "word/numbering.xml" not in self.archive.namelist():
            return
        root = self._read_xml("word/numbering.xml")
        abstracts = {}
        for abstract in root.findall("w:abstractNum", NS):
            levels = {}
            for level in abstract.findall("w:lvl", NS):
                levels[int(level.get(_q("w", "ilvl"), "0"))] = (
                    _val(level.find("w:numFmt", NS), "decimal"),
                    _val(level.find("w:lvlText", NS), "%1."),
                    int(_val(level.find("w:start", NS), "1")),
                )
            abstracts[abstract.get(_q("w", "abstractNumId"))] = levels
        for num in root.findall("w:num", NS):
            levels = dict(abstracts.get(_val(num.find("w:abstractNumId", NS)), {}))
            for override in num.findall("w:lvlOverride", NS):
                level = int(override.get(_q("w", "ilvl"), "0"))
                start = _val(override.find("w:startOverride", NS))
                if start is not None and level in levels:
                    fmt, pattern, _ = levels[level]
                    levels[level] = fmt, pattern, int(start)
            self.numbers[num.get(_q("w", "numId"))] = levels

    @staticmethod
    def _numeral(value, fmt):
        if fmt in {"lowerRoman", "upperRoman"} and 0 < value < 4000:
            remaining, parts = value, []
            for number, roman in ((1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
                                  (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
                                  (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I")):
                count, remaining = divmod(remaining, number)
                parts.append(roman * count)
            result = "".join(parts)
            return result.lower() if fmt == "lowerRoman" else result
        if fmt in {"lowerLetter", "upperLetter"} and value > 0:
            result = ""
            while value:
                value, remainder = divmod(value - 1, 26)
                result = chr(ord("a") + remainder) + result
            return result.upper() if fmt == "upperLetter" else result
        return f"{value:02}" if fmt == "decimalZero" else str(value)

    def _number_label(self, props):
        numpr = props.find("w:numPr", NS) if props is not None else None
        if numpr is None:
            return ""
        number = _val(numpr.find("w:numId", NS))
        level = int(_val(numpr.find("w:ilvl", NS), "0"))
        spec = self.numbers.get(number, {}).get(level)
        if spec is None:
            return ""
        fmt, pattern, start = spec
        key = (number, level)
        value = self.number_values.get(key, start - 1) + 1
        self.number_values[key] = value
        for old in list(self.number_values):
            if old[0] == number and old[1] > level:
                del self.number_values[old]
        if fmt == "bullet":
            return "• "
        def numeral(match):
            requested = int(match[1]) - 1
            val = self.number_values.get((number, requested), start)
            requested_format = self.numbers.get(number, {}).get(requested, (fmt, "", start))[0]
            return self._numeral(val, requested_format)
        return re.sub(r"%(\d+)", numeral, pattern) + " "

    def _image(self, node, paragraph, in_table):
        blips = node.findall(".//a:blip", NS)
        imagedata = node.findall(".//v:imagedata", NS)
        if not blips and not imagedata:
            return ""
        result = []
        for image in blips + imagedata:
            relationship = image.get(_q("r", "embed")) or image.get(_q("r", "id"))
            media = self.relationships.get(relationship)
            if media not in self.archive.namelist():
                self.diagnostic("OFFICE_IMAGE_MISSING", relationship=relationship, media=media)
                continue
            data = self.archive.read(media)
            mime = mimetypes.guess_type(media)[0] or "application/octet-stream"
            source_mime = mime
            self.image_index += 1
            extent = node.find(".//wp:extent", NS)
            display_width = display_height = None
            if extent is not None:
                display_width = round(int(extent.get("cx", "0")) / 9525, 2)
                display_height = round(int(extent.get("cy", "0")) / 9525, 2)
            docpr = node.find(".//wp:docPr", NS)
            text = "".join(paragraph.itertext()) if paragraph is not None else ""
            # w:t is source-visible text. instrText/field instructions are not.
            text = "".join(paragraph.xpath(".//w:t/text()", namespaces=NS)) if paragraph is not None else text
            width = height = None
            try:
                from PIL import Image
                metafile = media.lower().endswith(('.wmf', '.emf'))
                with (render_windows_metafile(data) if metafile else Image.open(BytesIO(data))) as source_image:
                    if mime not in {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}:
                        # Pillow's Windows metafile handler also supports EMF;
                        # browser img elements do not. Keep conversion in memory.
                        if source_image.format == "WMF":
                            source_image.load(dpi=192)
                        converted = BytesIO()
                        source_image.convert("RGBA").save(converted, "PNG")
                        data, mime = converted.getvalue(), "image/png"
                        self.diagnostic("OFFICE_IMAGE_RASTERIZED", media=media,
                                        source_media_type=source_mime, size=list(source_image.size))
                    width, height = source_image.size
            except Exception as exc:
                if mime not in {"image/png", "image/jpeg", "image/gif", "image/webp", "image/svg+xml"}:
                    self.diagnostic("OFFICE_IMAGE_RASTERIZATION_FAILED", media=media,
                                    error=type(exc).__name__, reason=str(exc))
                    raise RuntimeError(f'Cannot render Office asset {media}; no invisible image will be published') from exc
            metadata = {"index": self.image_index, "media_name": media, "relationship": relationship,
                        "width": width, "height": height, "display_width": display_width,
                        "display_height": display_height, "paragraph_text": text,
                        "inline": bool(node.find(".//wp:inline", NS) is not None or imagedata),
                        "in_table": in_table, "source_media_type": source_mime, "media_type": mime,
                        "title": docpr.get("title", "") if docpr is not None else "",
                        "description": docpr.get("descr", "") if docpr is not None else ""}
            if paragraph is not None:
                # Immediate source siblings only: a table or another block
                # breaks this context. Do not attach a distant definition to
                # an unrelated formula across an intervening source object.
                previous = paragraph.getprevious()
                following = paragraph.getnext()
                metadata["previous_text"] = ("".join(previous.xpath(".//w:t/text()", namespaces=NS))
                                             if previous is not None and previous.tag == _q("w", "p") else "")
                metadata["following_text"] = ("".join(following.xpath(".//w:t/text()", namespaces=NS))
                                              if following is not None and following.tag == _q("w", "p") else "")
                ordered = list(paragraph.iter())
                node_position = ordered.index(node)
                metadata["before"] = "".join(item.text or "" for item in ordered[:node_position] if item.tag == _q("w", "t"))
                descendants = set(node.iter())
                metadata["after"] = "".join(item.text or "" for item in ordered[node_position + 1:] if item not in descendants and item.tag == _q("w", "t"))
            mathml = self.recognize_formula(data, metadata) if self.recognize_formula else None
            if mathml:
                # A recognizer returns inert MathML. Never accept scripts/HTML.
                from bs4 import BeautifulSoup
                parsed = BeautifulSoup(mathml, "html.parser")
                math = parsed.find("math")
                if math is not None and not math.find(["script", "img", "iframe", "object"]):
                    math["display"] = "inline"
                    result.append(str(math))
                    self.stats["image_math"] += 1
                    self.diagnostic("OFFICE_IMAGE_RESULT", method="mathml", **metadata)
                    continue
            attributes = f' src="data:{mime};base64,{base64.b64encode(data).decode()}" alt="{html.escape(metadata["description"], quote=True)}"'
            if display_width:
                attributes += f' style="width:{display_width:g}px"'
            result.append("<img" + attributes + ">")
            self.stats["images"] += 1
            self.diagnostic("OFFICE_IMAGE_RESULT", method="source_image", **metadata)
        return "".join(result)

    def _math(self, node):
        from docling.backend.docx.latex.omml import oMath2Latex
        from latex2mathml.converter import convert
        if node.tag == _q("m", "oMathPara"):
            return "".join(self._math(child) for child in node if child.tag == _q("m", "oMath"))
        latex = str(oMath2Latex(node))
        if not latex.strip():
            return ""
        self.stats["native_math"] += 1
        self.diagnostic("OFFICE_NATIVE_MATH", latex=latex)
        return convert(latex, display="inline")

    def _symbol(self, node):
        """Decode well-defined Symbol characters, retain other font glyphs.

        Greek mapping is the Unicode Consortium's Adobe Symbol encoding table:
        https://www.unicode.org/Public/MAPPINGS/VENDORS/ADOBE/symbol.txt
        Unknown private-font glyphs keep their source font and code point;
        guessing a different mathematical symbol would change the document.
        """
        font = node.get(_q("w", "font"), "Symbol")
        code = int(node.get(_q("w", "char"), "0"), 16)
        low = code - 0xF000 if 0xF000 <= code <= 0xF0FF else code
        upper = dict(zip("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "ΑΒΧΔΕΦΓΗΙϑΚΛΜΝΟΠΘΡΣΤΥςΩΞΨΖ"))
        lower = dict(zip("abcdefghijklmnopqrstuvwxyz", "αβχδεφγηιϕκλμνοπθρστυϖωξψζ"))
        symbols = {0x22: "∀", 0x24: "∃", 0x27: "∋", 0x2A: "∗", 0x2D: "−", 0x40: "≅",
                   0x5C: "∴", 0x5E: "⊥", 0x7E: "∼", 0xA3: "≤", 0xA5: "∞", 0xB0: "°",
                   0xB1: "±", 0xB3: "≥", 0xB4: "×", 0xB5: "∝", 0xB6: "∂", 0xB8: "÷",
                   0xB9: "≠", 0xBA: "≡", 0xBB: "≈", 0xD0: "∠", 0xD1: "∇", 0xD5: "∏",
                   0xD6: "√", 0xD7: "⋅", 0xE5: "∑", 0xF2: "∫"}
        if font.lower() == "symbol":
            char = upper.get(chr(low)) or lower.get(chr(low)) or symbols.get(low)
            if char:
                self.stats["native_math"] += 1
                element = "mi" if char.isalpha() else "mo"
                return f'<math xmlns="http://www.w3.org/1998/Math/MathML" display="inline"><{element}>{char}</{element}></math>'
            if 0x20 <= low <= 0x7E:
                return html.escape(chr(low))
        safe_font = font if re.fullmatch(r"[\w -]{1,80}", font) else "serif"
        self.diagnostic("OFFICE_SYMBOL_RETAINED", font=font, source_code=f"{code:04X}")
        return '<span style="font-family:' + html.escape(safe_font, quote=True) + '">' + html.escape(chr(code)) + '</span>'

    def _inline(self, node, paragraph, in_table=False):
        tag = node.tag
        if tag in {_q("w", "del"), _q("w", "moveFrom"), _q("w", "instrText"), _q("w", "delText")}:
            return ""
        if tag == _q("w", "t"):
            return html.escape(node.text or "")
        if tag in {_q("w", "drawing"), _q("w", "pict")}:
            return self._image(node, paragraph, in_table)
        if tag in {_q("m", "oMath"), _q("m", "oMathPara")}:
            return self._math(node)
        if tag == _q("mc", "AlternateContent"):
            preferred = node.find("mc:Choice", NS)
            if preferred is None:
                preferred = node.find("mc:Fallback", NS)
            return "".join(self._inline(child, paragraph, in_table) for child in preferred) if preferred is not None else ""
        if tag == _q("w", "br") or tag == _q("w", "cr"):
            # Page/column breaks do not become extra intra-cell line breaks.
            if node.get(_q("w", "type"), "textWrapping") != "textWrapping":
                return ""
            self.stats["source_breaks"] += 1
            return "<br>"
        if tag == _q("w", "tab"):
            return '<span class="office-tab">\t</span>'
        if tag == _q("w", "noBreakHyphen"):
            return "‑"
        if tag == _q("w", "softHyphen"):
            return "\u00ad"
        if tag == _q("w", "sym"):
            return self._symbol(node)
        if tag in {_q("w", "footnoteReference"), _q("w", "endnoteReference")}:
            kind = "footnote" if _local(node) == "footnoteReference" else "endnote"
            number = node.get(_q("w", "id"), "")
            self.notes[kind].add(number)
            return "<sup>" + html.escape(number) + "</sup>"
        if tag == _q("w", "hyperlink"):
            self.stats["links_removed"] += 1
        if tag == _q("w", "r"):
            props = node.find("w:rPr", NS)
            if props is not None and (_on(props.find("w:vanish", NS)) or _on(props.find("w:webHidden", NS))):
                return ""
            value = "".join(self._inline(child, paragraph, in_table) for child in node if child.tag != _q("w", "rPr"))
            if props is not None:
                vertical = _val(props.find("w:vertAlign", NS))
                if vertical in {"subscript", "superscript"}:
                    wrapper = "sub" if vertical == "subscript" else "sup"
                    value = f"<{wrapper}>{value}</{wrapper}>"
                if _on(props.find("w:i", NS)):
                    value = "<em>" + value + "</em>"
                if _on(props.find("w:b", NS)):
                    value = "<strong>" + value + "</strong>"
                if _on(props.find("w:strike", NS)):
                    value = "<s>" + value + "</s>"
            return value
        if tag in {_q("w", "pPr"), _q("w", "rPr"), _q("w", "bookmarkStart"), _q("w", "bookmarkEnd")}:
            return ""
        return "".join(self._inline(child, paragraph, in_table) for child in node)

    def _paragraph(self, node, in_table=False):
        self.stats["paragraphs"] += 1
        props = node.find("w:pPr", NS)
        style = _val(props.find("w:pStyle", NS)) if props is not None else None
        level = self.styles.get(style)
        wrapper = f"h{level}" if level else "p"
        attributes = ""
        align = _val(props.find("w:jc", NS)) if props is not None else None
        if align in {"left", "right", "center", "both"}:
            attributes = f' style="text-align:{"justify" if align == "both" else align}"'
        label = self._number_label(props)
        content = ('<span class="office-list-label">' + html.escape(label) + "</span>") if label else ""
        content += "".join(self._inline(child, node, in_table) for child in node if child.tag != _q("w", "pPr"))
        if not content:
            return ""
        return f"<{wrapper}{attributes}>{content}</{wrapper}>"

    def _table(self, node):
        self.stats["tables"] += 1
        widths = []
        for column_node in node.findall("w:tblGrid/w:gridCol", NS):
            try:
                widths.append(max(0, int(column_node.get(_q("w", "w"), "0"))))
            except ValueError:
                widths.append(0)
        colgroup = ""
        if widths and sum(widths):
            total = sum(widths)
            colgroup = "<colgroup>" + "".join(f'<col style="width:{width / total * 100:.4g}%">' for width in widths) + "</colgroup>"
        rows = []
        # Store a reference to the anchor dict so continuations can extend it.
        previous = {}
        for row in node.findall("w:tr", NS):
            cells, current = [], {}
            grid_before = row.find("w:trPr/w:gridBefore", NS)
            column = int(_val(grid_before, "0"))
            if column:
                cells.append({"content": "", "colspan": column, "rowspan": 1, "header": False})
            header = _on(row.find("w:trPr/w:tblHeader", NS))
            extended = set()
            horizontal_anchor = None
            for cell in row.findall("w:tc", NS):
                props = cell.find("w:tcPr", NS)
                span = int(_val(props.find("w:gridSpan", NS), "1")) if props is not None else 1
                merge = props.find("w:vMerge", NS) if props is not None else None
                horizontal = props.find("w:hMerge", NS) if props is not None else None
                content = self._blocks(cell, in_table=True)
                anchor = previous.get(column)
                if horizontal is not None and _val(horizontal, "continue") != "restart" and horizontal_anchor:
                    horizontal_anchor["colspan"] += span
                    horizontal_anchor["content"] += content
                elif merge is not None and _val(merge, "continue") != "restart" and anchor and anchor["colspan"] == span:
                    if id(anchor) not in extended:
                        anchor["rowspan"] += 1
                        extended.add(id(anchor))
                    # Continuation content is unusual but must not disappear.
                    if content:
                        anchor["content"] += content
                    for offset in range(span):
                        current[column + offset] = anchor
                else:
                    entry = {"content": content, "colspan": span, "rowspan": 1, "header": header}
                    cells.append(entry)
                    horizontal_anchor = entry if horizontal is not None else None
                    if merge is not None:
                        for offset in range(span):
                            current[column + offset] = entry
                column += span
            rows.append(cells)
            previous = current
        rendered = []
        for row in rows:
            cells = []
            for cell in row:
                wrapper = "th" if cell["header"] else "td"
                attrs = ""
                for attr in ("rowspan", "colspan"):
                    if cell[attr] > 1:
                        attrs += f' {attr}="{cell[attr]}"'
                cells.append(f"<{wrapper}{attrs}>{cell['content']}</{wrapper}>")
            rendered.append("<tr>" + "".join(cells) + "</tr>")
        return '<div class="table-scroll"><table>' + colgroup + "".join(rendered) + "</table></div>"

    def _blocks(self, node, in_table=False):
        result = []
        for child in node:
            if child.tag == _q("w", "p"):
                result.append(self._paragraph(child, in_table))
            elif child.tag == _q("w", "tbl"):
                result.append(self._table(child))
            elif child.tag in {_q("w", "sdt"), _q("w", "sdtContent"), _q("w", "ins"), _q("w", "customXml")}:
                result.append(self._blocks(child, in_table))
        return "".join(result)

    def export(self):
        root = self._read_xml("word/document.xml")
        body = root.find("w:body", NS)
        content = self._blocks(body)
        for kind in ("footnote", "endnote"):
            part = f"word/{kind}s.xml"
            if not self.notes[kind] or part not in self.archive.namelist():
                continue
            relationships = self.relationships
            self.relationships = self._relationships(part)
            for note in self._read_xml(part):
                number = note.get(_q("w", "id"))
                if number in self.notes[kind]:
                    content += f'<div class="office-note"><sup>{html.escape(number)}</sup>' + self._blocks(note) + "</div>"
            self.relationships = relationships
        self.diagnostic("OFFICE_EXPORT_END", source=self.source_name, **self.stats)
        return ('<!DOCTYPE html><html lang="ru"><head><meta charset="utf-8"><title>' +
                html.escape(Path(self.source_name).stem) + "</title><style>" + CSS + "</style></head><body>" + content + "</body></html>", dict(self.stats))


def export_docx_html(path, recognize_formula=None, diagnostic=None, *, source_name=None):
    """Return (HTML, counters); recognizer receives (image_bytes, source_metadata)."""
    exporter = DocxHtmlExporter(path, recognize_formula, diagnostic, source_name=source_name)
    try:
        return exporter.export()
    finally:
        exporter.close()
