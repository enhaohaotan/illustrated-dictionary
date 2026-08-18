#!/usr/bin/env python3
"""Locate printed English entries and copy normalized PDF boxes to every database."""

from __future__ import annotations

import argparse
import difflib
import sqlite3
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Iterable, Optional, Sequence

import fitz


BBOX_COLUMNS = ("bbox_left", "bbox_top", "bbox_right", "bbox_bottom")
DEFAULT_DATABASES = (Path("en.sqlite3"), Path("zh.sqlite3"), Path("da.sqlite3"))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="source illustrated dictionary PDF")
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("en.sqlite3"),
        help="English database used for matching (default: en.sqlite3)",
    )
    parser.add_argument(
        "--database",
        type=Path,
        action="append",
        dest="databases",
        help="database to update; repeat for more languages",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report matching results without changing databases",
    )
    return parser.parse_args()


def normalized_token(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    return "".join(character for character in value if character.isalnum())


def source_tokens(value: str) -> list[str]:
    return [token for part in value.split() if (token := normalized_token(part))]


def anchored_text_rectangle(rectangles: Iterable[fitz.Rect]) -> fitz.Rect:
    """Cover all lines, but anchor the left edge to the first printed line."""
    parts = [fitz.Rect(rectangle) for rectangle in rectangles]
    rectangle = fitz.Rect(parts[0])
    for part in parts[1:]:
        rectangle.include_rect(part)
    first_line_y = min(part.y0 for part in parts)
    rectangle.x0 = min(
        part.x0 for part in parts if abs(part.y0 - first_line_y) <= 3.5
    )
    return rectangle


def word_occurrences(page: fitz.Page, source_text: str) -> list[fitz.Rect]:
    words = page.get_text("words", sort=False)
    wanted = source_tokens(source_text)
    if not wanted:
        return []

    normalized = [normalized_token(str(word[4])) for word in words]
    occurrences: list[fitz.Rect] = []
    wanted_text = "".join(wanted)
    for start in range(len(words)):
        candidate = ""
        for end in range(start, min(len(words), start + len(wanted) + 8)):
            candidate += normalized[end]
            if len(candidate) > len(wanted_text):
                break
            if candidate != wanted_text:
                continue
            selected = words[start : end + 1]
            occurrences.append(
                anchored_text_rectangle(fitz.Rect(word[:4]) for word in selected)
            )
            break
    return occurrences


def multiline_occurrences(page: fitz.Page, source_text: str) -> list[fitz.Rect]:
    lines = [line.strip() for line in source_text.splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    found = [(line, word_occurrences(page, line)) for line in lines]
    found = [(line, rectangles) for line, rectangles in found if rectangles]
    if not found:
        return []

    anchor_line, anchors = max(found, key=lambda item: len(normalized_token(item[0])))
    others = [item for item in found if item[0] != anchor_line]
    combined: list[fitz.Rect] = []
    for anchor in anchors:
        parts = [fitz.Rect(anchor)]
        for _line, candidates in others:
            nearby = min(
                candidates,
                key=lambda candidate: abs(candidate.x0 - anchor.x0)
                + 2 * abs(candidate.y0 - anchor.y0),
            )
            distance = abs(nearby.x0 - anchor.x0) + 2 * abs(nearby.y0 - anchor.y0)
            if distance <= 100:
                parts.append(fitz.Rect(nearby))
        combined.append(anchored_text_rectangle(parts))
    return combined


def geometric_occurrences(page: fitz.Page, source_text: str) -> list[fitz.Rect]:
    words = page.get_text("words", sort=False)
    normalized = [normalized_token(str(word[4])) for word in words]
    wanted = source_tokens(source_text)
    if len(wanted) < 2:
        return []

    paths: list[list[int]] = []

    def extend(path: list[int], wanted_index: int, line_start_x: float) -> None:
        if wanted_index == len(wanted):
            paths.append(path)
            return
        previous = fitz.Rect(words[path[-1]][:4])
        candidates: list[tuple[float, int]] = []
        for index, token in enumerate(normalized):
            if token != wanted[wanted_index] or index in path:
                continue
            candidate = fitz.Rect(words[index][:4])
            y_gap = candidate.y0 - previous.y0
            x_gap = candidate.x0 - previous.x1
            if abs(y_gap) <= 3.5 and -1.0 <= x_gap <= 28.0:
                candidates.append((max(0.0, x_gap), index))
                continue
            if 2.0 < y_gap <= 34.0 and abs(candidate.x0 - line_start_x) <= 58.0:
                candidates.append((40.0 + y_gap + abs(candidate.x0 - line_start_x), index))
        for _cost, index in sorted(candidates)[:6]:
            next_rect = fitz.Rect(words[index][:4])
            same_line = abs(next_rect.y0 - previous.y0) <= 3.5
            extend(path + [index], wanted_index + 1, line_start_x if same_line else next_rect.x0)

    for start, token in enumerate(normalized):
        if token == wanted[0]:
            extend([start], 1, fitz.Rect(words[start][:4]).x0)

    occurrences: list[fitz.Rect] = []
    seen: set[tuple[float, float, float, float]] = set()
    for path in paths:
        rectangle = anchored_text_rectangle(
            fitz.Rect(words[index][:4]) for index in path
        )
        key = tuple(round(value, 2) for value in rectangle)
        if key not in seen:
            seen.add(key)
            occurrences.append(rectangle)
    return occurrences


def fuzzy_single_word_occurrences(page: fitz.Page, source_text: str) -> list[fitz.Rect]:
    wanted = source_tokens(source_text)
    if len(wanted) != 1:
        return []
    matches: list[fitz.Rect] = []
    for word in page.get_text("words", sort=False):
        candidate = normalized_token(str(word[4]))
        if abs(len(candidate) - len(wanted[0])) > 1:
            continue
        if difflib.SequenceMatcher(None, wanted[0], candidate).ratio() >= 0.9:
            matches.append(fitz.Rect(word[:4]))
    return matches


def search_occurrences(page: fitz.Page, source_text: str) -> list[fitz.Rect]:
    rectangles = word_occurrences(page, source_text)
    if rectangles:
        return rectangles
    rectangles = multiline_occurrences(page, source_text)
    if rectangles:
        return rectangles
    rectangles = geometric_occurrences(page, source_text)
    if rectangles:
        return rectangles
    rectangles = fuzzy_single_word_occurrences(page, source_text)
    if rectangles:
        return rectangles
    return list(page.search_for(source_text, flags=fitz.TEXT_DEHYPHENATE))


def see_also_regions(page: fitz.Page) -> list[fitz.Rect]:
    return [
        fitz.Rect(block[:4])
        for block in page.get_text("blocks", sort=False)
        if "see also" in str(block[4]).casefold()
    ]


def overlaps_regions(rectangle: fitz.Rect, regions: Iterable[fitz.Rect]) -> bool:
    for region in regions:
        intersection = rectangle & region
        if intersection.width > 0 and intersection.height > 0:
            return True
    return False


def entry_occurrences(page: fitz.Page, source_text: str) -> list[fitz.Rect]:
    excluded_regions = see_also_regions(page)
    return [
        rectangle
        for rectangle in search_occurrences(page, source_text)
        if not overlaps_regions(rectangle, excluded_regions)
    ]


def occurrence_score(
    page: fitz.Page,
    rectangle: fitz.Rect,
    excluded_regions: Iterable[fitz.Rect] = (),
) -> tuple[float, float, float, float, float]:
    words = page.get_text("words", sort=False)
    left_neighbors: list[tuple[bool, float]] = []
    for word in words:
        word_rect = fitz.Rect(word[:4])
        same_line = abs(word_rect.y0 - rectangle.y0) <= max(3.0, rectangle.height)
        gap = rectangle.x0 - word_rect.x1
        if same_line and -0.5 <= gap <= 18.0:
            left_neighbors.append((normalized_token(str(word[4])).isdigit(), gap))
    has_number_marker = (
        1.0 if any(is_number for is_number, _gap in left_neighbors) else 0.0
    )
    has_marker = 1.0 if left_neighbors else 0.0
    nearest_gap = min((gap for _is_number, gap in left_neighbors), default=999.0)
    outside_excluded_regions = (
        0.0 if overlaps_regions(rectangle, excluded_regions) else 1.0
    )
    return (
        outside_excluded_regions,
        has_number_marker,
        has_marker,
        -nearest_gap,
        rectangle.y0,
    )


def conflicts_with_claimed(rectangle: fitz.Rect, claimed: list[fitz.Rect]) -> bool:
    area = rectangle.width * rectangle.height
    if area <= 0:
        return False
    for existing in claimed:
        intersection = rectangle & existing
        smaller_area = min(area, existing.width * existing.height)
        intersection_area = intersection.width * intersection.height
        if smaller_area > 0 and intersection_area / smaller_area >= 0.8:
            return True
    return False


def load_entries(connection: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    connection.row_factory = sqlite3.Row
    query = """
        SELECT
            e.id,
            e.source_text,
            e.bbox_left,
            e.bbox_top,
            e.bbox_right,
            e.bbox_bottom,
            s.page_id,
            s.number AS section_number
        FROM entries AS e
        JOIN sections AS s ON s.id = e.section_id
        ORDER BY s.page_id, e.id
    """
    by_page: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in connection.execute(query):
        by_page[row["page_id"]].append(row)
    return by_page


def stored_rectangle(entry: sqlite3.Row, page: fitz.Page) -> Optional[fitz.Rect]:
    if entry["bbox_left"] is None:
        return None
    return fitz.Rect(
        entry["bbox_left"] * page.rect.width,
        entry["bbox_top"] * page.rect.height,
        entry["bbox_right"] * page.rect.width,
        entry["bbox_bottom"] * page.rect.height,
    )


def occurrence_distance(rectangle: fitz.Rect, previous: fitz.Rect) -> float:
    return (
        abs(rectangle.y0 - previous.y0)
        + abs(rectangle.y1 - previous.y1)
        + abs(rectangle.x1 - previous.x1)
        + 0.1 * abs(rectangle.x0 - previous.x0)
    )


def preserve_existing_occurrence(
    rectangle: fitz.Rect, previous: Optional[fitz.Rect]
) -> fitz.Rect:
    if previous is None:
        return rectangle
    same_text_extent = (
        abs(rectangle.y0 - previous.y0) <= 1.0
        and abs(rectangle.x1 - previous.x1) <= 1.0
        and abs(rectangle.y1 - previous.y1) <= 1.0
    )
    return rectangle if same_text_extent else fitz.Rect(previous)


def normalized_box(rectangle: fitz.Rect, page: fitz.Page) -> tuple[float, ...]:
    return (
        max(0.0, min(1.0, rectangle.x0 / page.rect.width)),
        max(0.0, min(1.0, rectangle.y0 / page.rect.height)),
        max(0.0, min(1.0, rectangle.x1 / page.rect.width)),
        max(0.0, min(1.0, rectangle.y1 / page.rect.height)),
    )


def locate(
    pdf_path: Path, source: Path
) -> tuple[
    dict[int, tuple[float, ...]],
    list[str],
    int,
    set[int],
    dict[int, int],
]:
    source_connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    try:
        entries = load_entries(source_connection)
    finally:
        source_connection.close()

    expected_ids = {
        int(entry["id"]) for page_entries in entries.values() for entry in page_entries
    }
    boxes: dict[int, tuple[float, ...]] = {}
    unmatched: list[str] = []
    ambiguous = 0
    relocated_pages: dict[int, int] = {}
    claimed_by_page: dict[int, list[fitz.Rect]] = defaultdict(list)
    page_sections = {
        page_number: {entry["section_number"] for entry in page_entries}
        for page_number, page_entries in entries.items()
    }
    with fitz.open(pdf_path) as document:
        for page_number, page_entries in entries.items():
            if not 1 <= page_number <= document.page_count:
                raise ValueError(f"PDF page {page_number} is out of range")
            page = document[page_number - 1]
            groups: dict[str, list[sqlite3.Row]] = defaultdict(list)
            for entry in page_entries:
                groups[entry["source_text"]].append(entry)

            ordered_groups = sorted(
                groups.items(),
                key=lambda item: len(normalized_token(item[0])),
                reverse=True,
            )
            for source_text, group in ordered_groups:
                located_page = page
                occurrences = entry_occurrences(located_page, source_text)
                located_page_number = page_number
                if not occurrences:
                    section_number = group[0]["section_number"]
                    for candidate_page_number in (page_number - 1, page_number + 1):
                        if section_number not in page_sections.get(candidate_page_number, set()):
                            continue
                        candidate_page = document[candidate_page_number - 1]
                        occurrences = entry_occurrences(candidate_page, source_text)
                        if occurrences:
                            located_page = candidate_page
                            located_page_number = candidate_page_number
                            break
                if not occurrences:
                    for entry in group:
                        unmatched.append(
                            f"page {page_number}, entry {entry['id']}: {source_text}"
                        )
                    continue
                available = [
                    rectangle
                    for rectangle in occurrences
                    if not conflicts_with_claimed(
                        rectangle, claimed_by_page[located_page_number]
                    )
                ]
                if available:
                    occurrences = available
                if len(occurrences) > 1:
                    ambiguous += len(group)
                excluded_regions = see_also_regions(located_page)

                if len(group) > 1 and len(occurrences) >= len(group):
                    remaining = list(occurrences)
                    selected: list[tuple[sqlite3.Row, fitz.Rect]] = []
                    for entry in group:
                        previous = stored_rectangle(entry, located_page)
                        if previous is not None and overlaps_regions(
                            previous, excluded_regions
                        ):
                            previous = None
                        if previous is None:
                            rectangle = min(
                                remaining, key=lambda rect: (rect.y0, rect.x0)
                            )
                        else:
                            rectangle = min(
                                remaining,
                                key=lambda rect: occurrence_distance(rect, previous),
                            )
                        remaining.remove(rectangle)
                        rectangle = preserve_existing_occurrence(rectangle, previous)
                        selected.append((entry, rectangle))
                    for entry, rectangle in selected:
                        boxes[entry["id"]] = normalized_box(rectangle, located_page)
                        claimed_by_page[located_page_number].append(rectangle)
                        if located_page_number != page_number:
                            relocated_pages[entry["id"]] = located_page_number
                    continue

                previous = stored_rectangle(group[0], located_page)
                if previous is not None and overlaps_regions(
                    previous, excluded_regions
                ):
                    previous = None
                if previous is not None and len(occurrences) > 1:
                    rectangle = min(
                        occurrences,
                        key=lambda item: occurrence_distance(item, previous),
                    )
                else:
                    rectangle = max(
                        occurrences,
                        key=lambda item: occurrence_score(
                            located_page, item, excluded_regions
                        ),
                    )
                rectangle = preserve_existing_occurrence(rectangle, previous)
                for entry in group:
                    boxes[entry["id"]] = normalized_box(rectangle, located_page)
                    if located_page_number != page_number:
                        relocated_pages[entry["id"]] = located_page_number
                claimed_by_page[located_page_number].append(rectangle)
    return boxes, unmatched, ambiguous, expected_ids, relocated_pages


def ensure_columns(connection: sqlite3.Connection) -> None:
    existing = {
        row[1] for row in connection.execute("PRAGMA table_info(entries)").fetchall()
    }
    definitions = {
        "bbox_left": "REAL CHECK (bbox_left IS NULL OR bbox_left BETWEEN 0 AND 1)",
        "bbox_top": "REAL CHECK (bbox_top IS NULL OR bbox_top BETWEEN 0 AND 1)",
        "bbox_right": "REAL CHECK (bbox_right IS NULL OR bbox_right BETWEEN 0 AND 1)",
        "bbox_bottom": "REAL CHECK (bbox_bottom IS NULL OR bbox_bottom BETWEEN 0 AND 1)",
        "audio_url": "TEXT",
        "noun_marker": "TEXT",
    }
    for name, definition in definitions.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE entries ADD COLUMN {name} {definition}")


def update_databases(
    databases: Iterable[Path],
    boxes: dict[int, tuple[float, ...]],
    expected_ids: set[int],
    relocated_pages: dict[int, int],
) -> None:
    rows = [(*box, entry_id) for entry_id, box in boxes.items()]
    for database in databases:
        connection = sqlite3.connect(database)
        try:
            ensure_columns(connection)
            entry_ids = {row[0] for row in connection.execute("SELECT id FROM entries")}
            if entry_ids != expected_ids:
                missing = len(expected_ids - entry_ids)
                extra = len(entry_ids - expected_ids)
                raise ValueError(
                    f"{database} does not match source IDs "
                    f"({missing} missing, {extra} unexpected)"
                )
            for entry_id, target_page in relocated_pages.items():
                current = connection.execute(
                    """
                    SELECT s.number
                    FROM entries AS e
                    JOIN sections AS s ON s.id = e.section_id
                    WHERE e.id = ?
                    """,
                    (entry_id,),
                ).fetchone()
                target = connection.execute(
                    "SELECT id FROM sections WHERE page_id = ? AND number = ?",
                    (target_page, current[0]),
                ).fetchone()
                if target is None:
                    raise ValueError(
                        f"{database} has no section {current[0]} on page {target_page}"
                    )
                connection.execute(
                    "UPDATE entries SET section_id = ? WHERE id = ?",
                    (target[0], entry_id),
                )
            connection.execute(
                """
                UPDATE entries SET
                    bbox_left = NULL,
                    bbox_top = NULL,
                    bbox_right = NULL,
                    bbox_bottom = NULL
                """
            )
            connection.executemany(
                """
                UPDATE entries
                SET bbox_left = ?, bbox_top = ?, bbox_right = ?, bbox_bottom = ?
                WHERE id = ?
                """,
                rows,
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()


def main() -> int:
    args = parse_args()
    databases: Sequence[Path] = args.databases or DEFAULT_DATABASES
    boxes, unmatched, ambiguous, expected_ids, relocated_pages = locate(
        args.pdf, args.source
    )
    print(
        f"Located {len(boxes)} entries; "
        f"{len(unmatched)} unmatched; {ambiguous} had multiple occurrences; "
        f"{len(relocated_pages)} found on an adjacent spread page"
    )
    if unmatched:
        for item in unmatched[:30]:
            print(f"- {item}")
        if len(unmatched) > 30:
            print(f"- ... and {len(unmatched) - 30} more")
    if not args.dry_run:
        update_databases(databases, boxes, expected_ids, relocated_pages)
        print("Updated " + ", ".join(str(database) for database in databases))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
