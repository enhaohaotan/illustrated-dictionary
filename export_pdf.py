#!/usr/bin/env python3
"""Embed one language database as static text in the source PDF."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import fitz


ENTRY_FONT_SIZE = 7.0
ENTRY_LINE_HEIGHT = 7.7
WRAP_WIDTH = 120.0
TEXT_COLOR = (23 / 255, 33 / 255, 28 / 255)


@dataclass
class Label:
    text: str
    marker: str | None
    source: fitz.Rect
    lines: list[str]
    width: float
    height: float
    wraps: bool
    position: fitz.Rect | None = None


def _connect(database: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _font_name(language: str) -> str:
    if language.lower().startswith(("zh", "ja", "ko")):
        return "china-s"
    return "hebo"


def _text_width(font: fitz.Font, text: str, size: float = ENTRY_FONT_SIZE) -> float:
    return font.text_length(text, fontsize=size)


def _split_token(token: str, font: fitz.Font, max_width: float) -> list[str]:
    parts: list[str] = []
    current = ""
    for character in token:
        candidate = current + character
        if current and _text_width(font, candidate) > max_width:
            parts.append(current)
            current = character
        else:
            current = candidate
    if current:
        parts.append(current)
    return parts


def _wrap(text: str, font: fitz.Font, max_width: float) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        pieces = _split_token(word, font, max_width)
        for piece in pieces:
            candidate = f"{current} {piece}".strip()
            if current and _text_width(font, candidate) > max_width:
                lines.append(current)
                current = piece
            else:
                current = candidate
    if current:
        lines.append(current)
    return lines


def _overlap(first: fitz.Rect, second: fitz.Rect) -> float:
    width = max(0.0, min(first.x1, second.x1) - max(first.x0, second.x0))
    height = max(0.0, min(first.y1, second.y1) - max(first.y0, second.y0))
    return width * height


def _candidate_positions(
    source: fitz.Rect,
    width: float,
    height: float,
    page: fitz.Rect,
) -> Iterator[fitz.Rect]:
    vertical_step = height + 1
    seen: set[tuple[int, int]] = set()

    def candidate(left: float, top: float) -> fitz.Rect | None:
        rectangle = fitz.Rect(left, top, left + width, top + height)
        if not page.contains(rectangle):
            return None
        key = (round(left * 10), round(top * 10))
        if key in seen:
            return None
        seen.add(key)
        return rectangle

    for row in range(8):
        below = source.y1 + 1 + row * vertical_step
        above = source.y0 - height - 1 - row * vertical_step
        for column in range(6):
            shifts = (0,) if column == 0 else (-column * 12, column * 12)
            for shift in shifts:
                for left, top in (
                    (source.x0 + shift, below),
                    (source.x1 - width + shift, below),
                    (source.x0 + shift, above),
                    (source.x1 - width + shift, above),
                ):
                    rectangle = candidate(left, top)
                    if rectangle is not None:
                        yield rectangle
    for left in (source.x1 + 1, source.x0 - width - 1):
        rectangle = candidate(left, source.y0)
        if rectangle is not None:
            yield rectangle


def _source_box(row: sqlite3.Row, page: fitz.Page) -> fitz.Rect | None:
    if row["bbox_left"] is None:
        return None
    return fitz.Rect(
        row["bbox_left"] * page.rect.width,
        row["bbox_top"] * page.rect.height,
        row["bbox_right"] * page.rect.width,
        row["bbox_bottom"] * page.rect.height,
    )


def _printed_title(title: str) -> str:
    return re.sub(r"^\s*\d+(?:\.\d+)*\s+", "", title).strip()


def _title_box(page: fitz.Page, title: str) -> fitz.Rect | None:
    printed = _printed_title(title)
    rectangles = [
        rectangle
        for rectangle in page.search_for(printed)
        if rectangle.y0 < 80 and rectangle.height > 15
    ]
    if not rectangles:
        return None
    result = fitz.Rect(rectangles[0])
    for rectangle in rectangles[1:]:
        result.include_rect(rectangle)
    return result


def _section_box(page: fitz.Page, title: str) -> fitz.Rect | None:
    rectangles = sorted(
        (rectangle for rectangle in page.search_for(title) if 12 <= rectangle.height <= 22),
        key=lambda rectangle: (rectangle.y0, rectangle.x0),
    )
    if not rectangles:
        return None
    groups: list[list[fitz.Rect]] = []
    for rectangle in rectangles:
        if groups and rectangle.y0 <= max(part.y1 for part in groups[-1]) + 4:
            groups[-1].append(rectangle)
        else:
            groups.append([rectangle])
    parts = max(groups, key=lambda group: sum(part.width * part.height for part in group))
    result = fitz.Rect(parts[0])
    for rectangle in parts[1:]:
        result.include_rect(rectangle)
    return result


def _page_rows(
    source: sqlite3.Connection,
    translation: sqlite3.Connection,
    page_number: int,
) -> tuple[sqlite3.Row | None, sqlite3.Row | None, list[tuple[sqlite3.Row, sqlite3.Row]], list[tuple[sqlite3.Row, sqlite3.Row]]]:
    source_page = source.execute("SELECT id, title FROM pages WHERE id = ?", (page_number,)).fetchone()
    translated_page = translation.execute("SELECT id, title FROM pages WHERE id = ?", (page_number,)).fetchone()
    source_sections = source.execute(
        "SELECT id, number, title FROM sections WHERE page_id = ? ORDER BY id",
        (page_number,),
    ).fetchall()
    translated_sections = {
        row["id"]: row
        for row in translation.execute(
            "SELECT id, number, title FROM sections WHERE page_id = ? ORDER BY id",
            (page_number,),
        )
    }
    section_pairs = [
        (row, translated_sections[row["id"]])
        for row in source_sections
        if row["id"] in translated_sections
    ]
    query = """
        SELECT e.id, e.source_text, e.bbox_left, e.bbox_top, e.bbox_right,
               e.bbox_bottom, e.noun_marker
        FROM entries AS e
        JOIN sections AS s ON s.id = e.section_id
        WHERE s.page_id = ?
        ORDER BY e.id
    """
    source_entries = source.execute(query, (page_number,)).fetchall()
    translated_entries = {
        row["id"]: row for row in translation.execute(query, (page_number,))
    }
    entry_pairs = [
        (row, translated_entries[row["id"]])
        for row in source_entries
        if row["id"] in translated_entries
    ]
    return source_page, translated_page, section_pairs, entry_pairs


def _draw_heading(
    page: fitz.Page,
    text: str,
    point: fitz.Point,
    font_name: str,
    font: fitz.Font,
    font_size: float,
) -> fitz.Rect:
    width = font.text_length(text, fontsize=font_size)
    page.insert_text(
        point,
        text,
        fontname=font_name,
        fontsize=font_size,
        color=TEXT_COLOR,
        overlay=True,
    )
    return fitz.Rect(point.x, point.y - font_size, point.x + width, point.y + 1)


def _layout_entries(
    page: fitz.Page,
    entry_pairs: list[tuple[sqlite3.Row, sqlite3.Row]],
    protected: list[fitz.Rect],
    placed: list[fitz.Rect],
    bold_font: fitz.Font,
) -> list[Label]:
    labels: list[Label] = []
    number_boxes: list[fitz.Rect] = []
    for source_row, translated_row in entry_pairs:
        source_box = _source_box(source_row, page)
        if source_box is None:
            continue
        text = translated_row["source_text"].strip()
        marker = translated_row["noun_marker"]
        display = f"{text} {marker}" if marker else text
        wraps = len(text.split()) > 1 and len(text) > 24
        lines = _wrap(display, bold_font, WRAP_WIDTH) if wraps else [display]
        width = max(_text_width(bold_font, line) for line in lines)
        height = max(ENTRY_LINE_HEIGHT, len(lines) * ENTRY_LINE_HEIGHT)
        labels.append(Label(text, marker, source_box, lines, width, height, wraps))
        protected.append(
            fitz.Rect(source_box.x0, min(source_box.y1, source_box.y0 + 3), source_box.x1, source_box.y1)
        )
        number_boxes.append(
            fitz.Rect(max(0, source_box.x0 - 10), min(source_box.y1, source_box.y0 + 3), source_box.x0, source_box.y1)
        )

    for label in sorted(labels, key=lambda item: (-item.width, item.source.y0, item.source.x0)):
        starts_at_number = label.wraps or label.width > label.source.width + 5
        offset = 15 if label.wraps else 10
        anchor = fitz.Rect(label.source)
        if starts_at_number:
            anchor.x0 = max(0, anchor.x0 - offset)
        preferred_top = anchor.y1 + 1
        best: fitz.Rect | None = None
        best_collision = float("inf")
        best_cost = float("inf")
        for candidate in _candidate_positions(anchor, label.width, label.height, page.rect):
            collision = sum(_overlap(candidate, rectangle) for rectangle in protected)
            collision += sum(_overlap(candidate, rectangle) for rectangle in number_boxes)
            collision += sum(_overlap(candidate, rectangle + (-0.5, -0.5, 0.5, 0.5)) for rectangle in placed)
            cost = abs(candidate.x0 - anchor.x0) + abs(candidate.y0 - preferred_top) * 12
            if collision < best_collision or (collision == best_collision and cost < best_cost):
                best = candidate
                best_collision = collision
                best_cost = cost
            if collision == 0 and abs(candidate.x0 - anchor.x0) <= 1 and abs(candidate.y0 - preferred_top) <= 1:
                break
        label.position = best
        if best is not None:
            placed.append(best)
    return labels


def _draw_label(
    page: fitz.Page,
    label: Label,
    font_name: str,
    bold_font: fitz.Font,
) -> None:
    if label.position is None:
        return
    for index, line in enumerate(label.lines):
        baseline = label.position.y0 + ENTRY_FONT_SIZE + index * ENTRY_LINE_HEIGHT
        marker = label.marker if index == len(label.lines) - 1 else None
        prefix = line[:-len(marker)] if marker and line.endswith(marker) else line
        page.insert_text(
            (label.position.x0, baseline),
            prefix,
            fontname=font_name,
            fontsize=ENTRY_FONT_SIZE,
            color=TEXT_COLOR,
            overlay=True,
        )
        if marker and line.endswith(marker):
            page.insert_text(
                (label.position.x0 + _text_width(bold_font, prefix), baseline),
                marker,
                fontname="heit" if font_name == "hebo" else font_name,
                fontsize=ENTRY_FONT_SIZE,
                color=TEXT_COLOR,
                overlay=True,
            )


def export_translated_pdf(
    source_pdf: Path,
    source_database: Path,
    translation_database: Path,
    output_pdf: Path,
) -> None:
    font_name = _font_name(translation_database.stem)
    bold_font = fitz.Font(fontname=font_name)
    output_pdf.parent.mkdir(parents=True, exist_ok=True)

    source = _connect(source_database)
    translation = _connect(translation_database)
    document = fitz.open(source_pdf)
    try:
        for page_number in range(1, document.page_count + 1):
            source_page, translated_page, section_pairs, entry_pairs = _page_rows(
                source, translation, page_number
            )
            if source_page is None or translated_page is None:
                continue
            page = document[page_number - 1]
            protected: list[fitz.Rect] = []
            placed: list[fitz.Rect] = []

            title_rectangle = _title_box(page, source_page["title"])
            if title_rectangle is not None:
                protected.append(title_rectangle)
                title = _printed_title(translated_page["title"])
                rectangle = _draw_heading(
                    page,
                    title,
                    fitz.Point(title_rectangle.x0, title_rectangle.y1 + 11),
                    font_name,
                    bold_font,
                    11,
                )
                placed.append(rectangle)

            for source_section, translated_section in section_pairs:
                rectangle = _section_box(page, source_section["title"])
                if rectangle is None:
                    continue
                protected.append(rectangle)
                title = _printed_title(translated_section["title"])
                point = fitz.Point(rectangle.x1 + 6, rectangle.y0 + rectangle.height / 2 + 4)
                placed.append(
                    _draw_heading(page, title, point, font_name, bold_font, 10)
                )

            labels = _layout_entries(page, entry_pairs, protected, placed, bold_font)
            for label in labels:
                _draw_label(page, label, font_name, bold_font)

        metadata = document.metadata
        metadata["subject"] = f"{metadata.get('subject', '')} Embedded translation: {translation_database.stem}".strip()
        document.set_metadata(metadata)
        document.save(output_pdf, garbage=0, deflate=False)
    finally:
        document.close()
        source.close()
        translation.close()
