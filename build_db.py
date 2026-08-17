#!/usr/bin/env python3

import argparse
import json
import re
import sqlite3
from pathlib import Path


SECTION_RE = re.compile(r"^(\d+\.\d+)\s+(.+)$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build en.sqlite3 from the extracted page JSON files."
    )
    parser.add_argument(
        "--input-dir",
        type=Path,
        default=Path("output"),
        help="Directory containing pages_*.json (default: output)",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=Path("en.sqlite3"),
        help="Database to create (default: en.sqlite3)",
    )
    return parser.parse_args()


def load_pages(input_dir: Path) -> list[dict]:
    paths = sorted(input_dir.glob("pages_*.json"))
    if not paths:
        raise SystemExit(f"No pages_*.json files found in {input_dir}")

    pages: list[dict] = []
    for path in paths:
        with path.open(encoding="utf-8") as file:
            document = json.load(file)
        pages.extend(document["pages"])

    pages.sort(key=lambda page: page["page"])
    page_ids = [page["page"] for page in pages]
    if len(page_ids) != len(set(page_ids)):
        raise SystemExit("Duplicate PDF pages found in input JSON")
    return pages


def split_section(value: str) -> tuple[str, str]:
    match = SECTION_RE.fullmatch(value)
    if not match:
        raise SystemExit(f"Invalid section format: {value!r}")
    return match.group(1), match.group(2)


def build_database(pages: list[dict], database: Path, schema: Path) -> None:
    database.parent.mkdir(parents=True, exist_ok=True)
    temporary = database.with_name(f".{database.name}.tmp")
    temporary.unlink(missing_ok=True)

    connection = sqlite3.connect(temporary)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(schema.read_text(encoding="utf-8"))

        section_id = 0
        entry_id = 0
        for page in pages:
            page_id = page["page"]
            title = page.get("title")
            if not title:
                raise SystemExit(f"Page {page_id} has no title")

            see_also = json.dumps(
                page.get("see_also", []),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            connection.execute(
                "INSERT INTO pages (id, title, see_also) VALUES (?, ?, ?)",
                (page_id, title, see_also),
            )

            page_sections: dict[str, int] = {}
            for entry in page["entries"]:
                section = entry.get("section")
                if not section:
                    raise SystemExit(
                        f"Page {page_id} entry {entry.get('source_text')!r} has no section"
                    )

                if section not in page_sections:
                    section_number, section_title = split_section(section)
                    section_id += 1
                    page_sections[section] = section_id
                    connection.execute(
                        """
                        INSERT INTO sections (id, page_id, number, title)
                        VALUES (?, ?, ?, ?)
                        """,
                        (section_id, page_id, section_number, section_title),
                    )

                entry_id += 1
                connection.execute(
                    """
                    INSERT INTO entries (
                        id,
                        section_id,
                        number,
                        source_text,
                        forms,
                        visual_context,
                        semantic_meaning
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        entry_id,
                        page_sections[section],
                        entry.get("number"),
                        entry["source_text"],
                        None,
                        entry.get("visual_context"),
                        entry.get("semantic_meaning"),
                    ),
                )

        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise SystemExit(f"Foreign key violations: {violations}")

        connection.commit()
    except BaseException:
        connection.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        connection.close()
        temporary.replace(database)


def main() -> None:
    args = parse_args()
    script_dir = Path(__file__).resolve().parent
    schema = script_dir / "schema.sql"
    pages = load_pages(args.input_dir)
    build_database(pages, args.database, schema)

    connection = sqlite3.connect(args.database)
    try:
        counts = {
            table: connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("pages", "sections", "entries")
        }
    finally:
        connection.close()

    print(
        f"Created {args.database}: "
        f"{counts['pages']} pages, "
        f"{counts['sections']} sections, "
        f"{counts['entries']} entries"
    )


if __name__ == "__main__":
    main()
