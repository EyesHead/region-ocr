"""Conservative PDF flow repairs using retained layout and source glyphs.

The rules depend on physical layout and table structure, never on a document
name, table identifier or expected wording.
"""
import re

from bs4 import BeautifulSoup


def _key(text):
    return re.sub(r"[\s\u00ad]+", "", text).casefold()


def _source_text(reader, page_no, box, api):
    if reader is None:
        return None
    reader.load_page(page_no)
    glyphs = [g for g in reader.chars if g.get("text", "").strip()
              and box[0] - 1 <= (g["x0"] + g["x1"]) / 2 <= box[2] + 1
              and box[1] - 1 <= (g["top"] + g["bottom"]) / 2 <= box[3] + 1]
    if not glyphs:
        return None
    return " ".join(line["text"] for line in api.native_cell_lines(glyphs))


def recover_caption_titles(doc, reader, excluded, manifest, api):
    """Retain a source-verified title mislabeled as furniture after a caption.

    A unique short line in the content area, directly after a recovered table
    label and before its table, belongs to the caption when its exact native
    glyphs agree. This also handles a label/title printed on the preceding page.
    The physical bottom margin, repeated furniture and prose are excluded.
    """
    from docling_core.types.doc import ContentLayer, DocItemLabel

    recovered = []
    pages_by_key = {}
    for text in doc.texts:
        region = api.item_region(text, doc)
        if region:
            pages_by_key.setdefault(_key(text.text), set()).add(region[0])
    for entry in manifest.get("recovered_table_captions", []):
        if not entry.get("table"):
            continue
        caption = doc.texts[int(entry["ref"].rsplit("/", 1)[1])]
        table = doc.tables[int(entry["table"].rsplit("/", 1)[1])]
        cap_reg, table_reg = api.item_region(caption, doc), api.item_region(table, doc)
        if not cap_reg or not table_reg:
            continue
        page_no, cap_box = cap_reg
        table_page, table_box = table_reg
        height, width = doc.pages[page_no].size.height, doc.pages[page_no].size.width
        if table_page not in {page_no, page_no + 1}:
            continue
        if table_page != page_no and table_box[1] > doc.pages[table_page].size.height * .25:
            continue
        candidates = []
        for text in doc.texts:
            if text.label not in {DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER}:
                continue
            reg = api.item_region(text, doc)
            value = text.text.strip()
            if not reg or reg[0] != page_no or len(value) > 250:
                continue
            box = reg[1]
            if (not 0 <= box[1] - cap_box[3] <= 80
                    or box[3] > height * .92 or box[3] - box[1] > height * .08
                    or box[2] - box[0] < width * .25
                    or (table_page == page_no and box[3] > table_box[1] + 2)
                    or len(pages_by_key.get(_key(value), ())) != 1
                    or sum(c.isalpha() for c in value) < 8
                    or re.search(r"(?:https?://|www\.|@|[.!?;:]\s*$)", value, re.I)
                    or api.is_document_designation(value)):
                continue
            native = _source_text(reader, page_no, box, api)
            if native is not None and _key(native) == _key(value):
                candidates.append((box[1], text, box))
        if len(candidates) != 1:
            continue
        _, title, title_box = candidates[0]
        bundle = [(title, title_box)]
        # A short parenthetical immediately following the title is part of
        # the same caption block; retain its place when moving the title.
        for text in doc.texts:
            reg = api.item_region(text, doc)
            value = text.text.strip()
            if (text.label == DocItemLabel.TEXT and text.self_ref not in excluded
                    and reg and reg[0] == page_no and 0 <= reg[1][1] - title_box[3] <= 18
                    and reg[1][3] <= height * .94 and len(value) <= 250
                    and re.fullmatch(r"\([^\n]+\)", value)):
                native = _source_text(reader, page_no, reg[1], api)
                if native is not None and _key(native) == _key(value):
                    bundle.append((text, reg[1]))
        for text, box in bundle:
            if text.parent:
                parent = text.parent.resolve(doc)
                parent.children = [ref for ref in parent.children if ref.cref != text.self_ref]
            text.parent = table.get_ref()
            text.label, text.content_layer = DocItemLabel.CAPTION, ContentLayer.BODY
            if not any(ref.cref == text.self_ref for ref in table.children):
                table.children.append(text.get_ref())
            if not any(ref.cref == text.self_ref for ref in table.captions):
                table.captions.append(text.get_ref())
            excluded.discard(text.self_ref)
            recovered.append({"ref": text.self_ref, "table": table.self_ref, "page": page_no,
                              "bbox": box, "text": text.text, "method": "caption_sequence_and_native_glyphs"})
        def caption_order(ref):
            reg = api.item_region(ref.resolve(doc), doc)
            return (reg[0], reg[1][1]) if reg else (0, 0)
        table.captions.sort(key=caption_order)
    manifest["recovered_table_titles"] = recovered


def _flow_region(item, doc, excluded, api, visiting=None):
    """A group has source bounds only if all visible descendants share a page."""
    from docling_core.types.doc import ContentLayer

    if item.self_ref in excluded or getattr(item, "content_layer", None) == ContentLayer.FURNITURE:
        return None
    prov = getattr(item, "prov", None)
    if prov:
        if len({p.page_no for p in prov}) != 1:
            return None
        boxes = [p.bbox.to_top_left_origin(doc.pages[p.page_no].size.height) for p in prov]
        return prov[0].page_no, [min(b.l for b in boxes), min(b.t for b in boxes),
                               max(b.r for b in boxes), max(b.b for b in boxes)]
    refs = getattr(item, "children", None)
    if not refs:
        return None
    visiting = set() if visiting is None else visiting
    if item.self_ref in visiting:
        return None
    visiting.add(item.self_ref)
    regions = []
    for ref in refs:
        child = ref.resolve(doc)
        if child.self_ref in excluded or getattr(child, "content_layer", None) == ContentLayer.FURNITURE:
            continue
        reg = _flow_region(child, doc, excluded, api, visiting)
        if reg is None:
            return None
        regions.append(reg)
    visiting.discard(item.self_ref)
    if not regions or len({r[0] for r in regions}) != 1:
        return None
    return regions[0][0], [min(r[1][0] for r in regions), min(r[1][1] for r in regions),
                          max(r[1][2] for r in regions), max(r[1][3] for r in regions)]


def restore_table_reading_order(doc, excluded, manifest, api):
    """Sort only inverted, nonoverlapping intervals containing a wide table.

    Lists retain their internal structure. Their one-page descendant bounds
    allow a list definition to move before a later table without flattening it
    or globally sorting columns and unrelated paragraphs.
    """
    from docling_core.types.doc import ContentLayer

    repairs = []
    for parent in [doc.body, *doc.groups]:
        regions = {ref.cref: _flow_region(ref.resolve(doc), doc, excluded, api) for ref in parent.children}
        changed = True
        while changed:
            changed = False
            children = [ref.resolve(doc) for ref in parent.children]
            for index, item in enumerate(children):
                reg = regions[item.self_ref]
                if not reg:
                    continue
                page_no, box = reg
                for other_index in range(index + 1, len(children)):
                    other_reg = regions[children[other_index].self_ref]
                    if not other_reg or other_reg[0] != page_no or box[1] < other_reg[1][3] - 2:
                        continue
                    window = children[index:other_index + 1]
                    visible = [(node, regions[node.self_ref]) for node in window
                               if node.self_ref not in excluded
                               and getattr(node, "content_layer", None) != ContentLayer.FURNITURE]
                    if (any(not loc or loc[0] != page_no for _, loc in visible)
                            or not any(node.self_ref.startswith("#/tables/")
                                       and loc[1][2] - loc[1][0] >= .7 * doc.pages[page_no].size.width
                                       for node, loc in visible)):
                        continue
                    ordered = sorted(visible, key=lambda pair: pair[1][1][1])
                    if any(a[1][1][3] > b[1][1][1] + 2 for a, b in zip(ordered, ordered[1:])):
                        continue
                    if [node.self_ref for node, _ in visible] == [node.self_ref for node, _ in ordered]:
                        continue
                    refs = iter(node.get_ref() for node, _ in ordered)
                    visible_refs = {node.self_ref for node, _ in visible}
                    for offset, node in enumerate(window):
                        if node.self_ref in visible_refs:
                            parent.children[index + offset] = next(refs)
                    repairs.append({"page": page_no, "parent": parent.self_ref,
                                    "before": [node.self_ref for node, _ in visible],
                                    "after": [node.self_ref for node, _ in ordered]})
                    changed = True
                    break
                if changed:
                    break
    manifest["table_reading_order_repairs"] = repairs


def _integer_keys(profile):
    return [int(match[1]) for c in profile.get("cells", [])
            if c["column"] == 0 and c["column_span"] == 1
            and (match := re.match(r"^(\d{1,6})(?:[.)]?\s|$)", c["text"].strip()))]


def join_same_page_sections(doc, routes, excluded, note_routes, manifest, api):
    """Rejoin ruled sections separated by a source-verified spanning heading.

    Require a retained OCR table enclosing both physical grids, an intervening
    source-glyph heading recovered from its full-width OCR row, identical
    physical columns and consecutive printed row identifiers. Independent
    neighboring tables cannot satisfy these conditions merely by width.
    """
    from docling_core.types.doc import TableCell, TableData

    entries = {e["ref"]: e for e in manifest.get("tables", [])}
    joins = manifest.setdefault("joined_tables", [])
    owner = {e["from"]: e["into"] for e in joins}
    def root(ref):
        seen = set()
        while ref in owner and ref not in seen:
            seen.add(ref); ref = owner[ref]
        return ref
    merged = []
    for heading in manifest.get("recovered_ocr_table_blocks", []):
        above, below = heading.get("after_table"), heading.get("before_table")
        if not above or not below or above not in entries or below not in entries:
            continue
        a, b = entries[above], entries[below]
        ga, gb = a.get("join_geometry"), b.get("join_geometry")
        if a["page"] != b["page"] or a["page"] != heading["page"] or not ga or not gb:
            continue
        source = heading.get("source_record_bbox")
        if (not source or not ga["bbox"][3] <= heading["bbox"][1] <= heading["bbox"][3] <= gb["bbox"][1]
                or any(box[0] < source[0] - 4 or box[1] < source[1] - 4
                       or box[2] > source[2] + 4 or box[3] > source[3] + 4 for box in (ga["bbox"], gb["bbox"]))
                or len(ga["x_grid"]) != len(gb["x_grid"])
                or any(abs(x - y) > 2 for x, y in zip(ga["x_grid"], gb["x_grid"]))):
            continue
        target, current = root(above), root(below)
        if target == current or current != below:
            continue
        left = BeautifulSoup(routes.get(target, ""), "html.parser")
        right = BeautifulSoup(routes.get(current, ""), "html.parser")
        if not left.table or not right.table:
            continue
        lp, rp = api.table_profile(str(left.table)), api.table_profile(str(right.table))
        keys_left, keys_right = _integer_keys(lp), _integer_keys(rp)
        if (lp["issues"] or rp["issues"] or lp["columns"] != rp["columns"]
                or not keys_left or not keys_right or keys_right[0] != keys_left[-1] + 1):
            continue
        fragment = note_routes.get(heading["ref"]) or heading.get("html", "")
        content = BeautifulSoup(fragment, "html.parser")
        if content.find(["table", "img", "math"]) or _key(content.get_text()) != _key(heading["text"]):
            continue
        body = left.table.find_all("tbody", recursive=False)
        body = body[-1] if body else left.table
        row = left.new_tag("tr")
        cell = left.new_tag("th", colspan=str(lp["columns"]), scope="colgroup")
        for node in list(content.contents):
            cell.append(node.extract())
        row.append(cell); body.append(row)
        for row in list(right.table.find_all("tr")):
            for cell in row.find_all("th"):
                cell.name = "td"
            body.append(row.extract())
        api.normalize_joined_table_body(left.table)
        combined = str(left)
        profile = api.table_profile(str(left.table), lp["columns"])
        if profile["issues"]:
            continue
        routes[target], routes[current] = combined, ""
        target_item = doc.tables[int(target.rsplit("/", 1)[1])]
        current_item = doc.tables[int(current.rsplit("/", 1)[1])]
        target_item.data = TableData(num_rows=profile["rows"], num_cols=profile["columns"], table_cells=[
            TableCell(text=c["text"], row_span=c["row_span"], col_span=c["column_span"],
                      start_row_offset_idx=c["row"], end_row_offset_idx=c["row"] + c["row_span"],
                      start_col_offset_idx=c["column"], end_col_offset_idx=c["column"] + c["column_span"],
                      column_header=c["header"]) for c in profile["cells"]])
        current_item.data = TableData(num_rows=0, num_cols=0, table_cells=[])
        target_item.prov.extend(prov.model_copy(deep=True) for prov in current_item.prov)
        entries[target]["selected"], entries[target]["html"] = profile, combined
        entries[target]["last_join_geometry"] = gb
        entries[current]["joined_into"] = target
        excluded.add(heading["ref"])
        owner[current] = target
        event = {"into": target, "from": current, "page": b["page"],
                 "basis": "same_page_grids_shared_ocr_spanning_heading_and_consecutive_row_keys",
                 "heading": heading["ref"], "rows": profile["rows"], "columns": profile["columns"]}
        joins.append(event); merged.append(event)
    manifest["same_page_table_sections"] = merged
