#!/usr/bin/env python3
"""Local web API for the illustrated multilingual dictionary."""

from __future__ import annotations

import argparse
import os
import re
import sqlite3
from functools import lru_cache
from pathlib import Path
from typing import Any

import fitz
import uvicorn
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles


ROOT = Path(__file__).resolve().parent
DEFAULT_PDF = ROOT / "EnglishforEveryoneIllustratedEnglishDictionary.pdf"
WEB_DIR = ROOT / "web"
LANGUAGE_NAMES = {"en": "English", "da": "Dansk", "zh": "中文"}


@lru_cache(maxsize=512)
def title_box(pdf_path: Path, page_number: int, title: str) -> dict[str, float] | None:
    printed_title = re.sub(r"^\s*\d+(?:\.\d+)*\s+", "", title).strip()
    if not printed_title:
        return None

    with fitz.open(pdf_path) as document:
        page = document[page_number - 1]
        rectangles = [
            rectangle
            for rectangle in page.search_for(printed_title)
            if rectangle.y0 < 80 and rectangle.height > 15
        ]
        if not rectangles:
            return None
        rectangle = fitz.Rect(rectangles[0])
        for part in rectangles[1:]:
            rectangle.include_rect(part)
        return {
            "left": rectangle.x0 / page.rect.width,
            "top": rectangle.y0 / page.rect.height,
            "right": rectangle.x1 / page.rect.width,
            "bottom": rectangle.y1 / page.rect.height,
        }


@lru_cache(maxsize=2048)
def section_box(
    pdf_path: Path, page_number: int, title: str
) -> dict[str, float] | None:
    with fitz.open(pdf_path) as document:
        page = document[page_number - 1]
        rectangles = sorted(
            (
                rectangle
                for rectangle in page.search_for(title)
                if 12 <= rectangle.height <= 22
            ),
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
        parts = max(
            groups,
            key=lambda group: sum(part.width * part.height for part in group),
        )
        rectangle = fitz.Rect(parts[0])
        for part in parts[1:]:
            rectangle.include_rect(part)
        return {
            "left": rectangle.x0 / page.rect.width,
            "top": rectangle.y0 / page.rect.height,
            "right": rectangle.x1 / page.rect.width,
            "bottom": rectangle.y1 / page.rect.height,
        }


def database_paths() -> dict[str, Path]:
    paths: dict[str, Path] = {}
    for path in ROOT.glob("*.sqlite3"):
        code = path.stem
        if re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]+)*", code):
            paths[code] = path
    return dict(sorted(paths.items(), key=lambda item: (item[0] != "en", item[0])))


def read_rows(database: Path, query: str, parameters: tuple[Any, ...]) -> list[dict]:
    connection = sqlite3.connect(f"file:{database.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in connection.execute(query, parameters)]
    finally:
        connection.close()


@lru_cache(maxsize=8)
def page_range(database: Path) -> tuple[int, int]:
    rows = read_rows(database, "SELECT MIN(id) AS first, MAX(id) AS last FROM pages", ())
    return int(rows[0]["first"]), int(rows[0]["last"])


def audio_response(language: str, entry_id: int) -> Response:
    databases = database_paths()
    database = databases.get(language)
    if database is None:
        raise HTTPException(404, "Unknown language")
    rows = read_rows(
        database, "SELECT audio_url FROM entries WHERE id = ?", (entry_id,)
    )
    if not rows:
        raise HTTPException(404, "Unknown entry")
    audio_url = rows[0]["audio_url"]
    if not audio_url:
        raise HTTPException(404, "Audio has not been generated yet")
    if re.match(r"^https?://", audio_url):
        return RedirectResponse(audio_url)

    audio_root = (ROOT / "audio").resolve()
    path = (audio_root / audio_url).resolve()
    if audio_root not in path.parents or not path.is_file():
        raise HTTPException(404, "Audio file not found")
    return FileResponse(path)


def create_app(pdf_path: Path = DEFAULT_PDF) -> FastAPI:
    app = FastAPI(title="Illustrated Dictionary Reader")

    @app.get("/api/config")
    def config() -> dict:
        databases = database_paths()
        if "en" not in databases:
            raise HTTPException(500, "en.sqlite3 is missing")
        first_page, last_page = page_range(databases["en"])
        return {
            "first_page": first_page,
            "last_page": last_page,
            "languages": [
                {"code": code, "name": LANGUAGE_NAMES.get(code, code)}
                for code in databases
            ],
        }

    @app.get("/api/pdf")
    def pdf() -> FileResponse:
        if not pdf_path.is_file():
            raise HTTPException(404, "PDF file not found")
        return FileResponse(pdf_path, media_type="application/pdf", filename=pdf_path.name)

    @app.get("/api/pdf/pages/{page_number}.png")
    def pdf_page(
        page_number: int,
        scale: float = Query(default=2.0, ge=1.0, le=4.0),
    ) -> Response:
        if not pdf_path.is_file():
            raise HTTPException(404, "PDF file not found")
        with fitz.open(pdf_path) as document:
            if not 1 <= page_number <= document.page_count:
                raise HTTPException(404, "PDF page out of range")
            page = document[page_number - 1]
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            image = pixmap.tobytes("png")
        return Response(
            content=image,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/api/pages/{page_number}")
    def page_data(page_number: int, language: str = "da") -> dict:
        databases = database_paths()
        if language not in databases:
            raise HTTPException(404, "Unknown language")
        if "en" not in databases:
            raise HTTPException(500, "en.sqlite3 is missing")

        first_page, last_page = page_range(databases["en"])
        if not first_page <= page_number <= last_page:
            raise HTTPException(404, "Page has no dictionary data")

        query = """
            SELECT
                e.id,
                e.number,
                e.source_text,
                e.bbox_left,
                e.bbox_top,
                e.bbox_right,
                e.bbox_bottom,
                e.audio_url,
                e.noun_marker,
                s.number AS section_number,
                s.title AS section_title,
                p.title AS page_title
            FROM entries AS e
            JOIN sections AS s ON s.id = e.section_id
            JOIN pages AS p ON p.id = s.page_id
            WHERE p.id = ?
            ORDER BY e.id
        """
        originals = read_rows(databases["en"], query, (page_number,))
        translations = {
            row["id"]: row
            for row in read_rows(databases[language], query, (page_number,))
        }
        source_page = read_rows(
            databases["en"], "SELECT title FROM pages WHERE id = ?", (page_number,)
        )
        translated_page = read_rows(
            databases[language], "SELECT title FROM pages WHERE id = ?", (page_number,)
        )
        source_title = source_page[0]["title"] if source_page else None
        translated_title = translated_page[0]["title"] if translated_page else None
        section_query = """
            SELECT id, number, title
            FROM sections
            WHERE page_id = ?
            ORDER BY id
        """
        source_sections = read_rows(
            databases["en"], section_query, (page_number,)
        )
        translated_sections = {
            row["id"]: row
            for row in read_rows(databases[language], section_query, (page_number,))
        }
        sections = []
        for source_section in source_sections:
            translated_section = translated_sections.get(source_section["id"])
            if translated_section is None:
                continue
            sections.append(
                {
                    "id": source_section["id"],
                    "number": source_section["number"],
                    "source_title": source_section["title"],
                    "title": translated_section["title"],
                    "bbox": section_box(
                        pdf_path.resolve(),
                        page_number,
                        source_section["title"],
                    ),
                }
            )
        entries = []
        for original in originals:
            translated = translations.get(original["id"])
            if translated is None:
                continue
            entries.append(
                {
                    "id": original["id"],
                    "number": original["number"],
                    "source_text": original["source_text"],
                    "translation": translated["source_text"],
                    "translation_noun_marker": translated["noun_marker"],
                    "section": {
                        "number": translated["section_number"],
                        "title": translated["section_title"],
                    },
                    "bbox": (
                        {
                            "left": original["bbox_left"],
                            "top": original["bbox_top"],
                            "right": original["bbox_right"],
                            "bottom": original["bbox_bottom"],
                        }
                        if original["bbox_left"] is not None
                        else None
                    ),
                    "source_audio": (
                        f"/api/audio/en/{original['id']}"
                        if original["audio_url"]
                        else None
                    ),
                    "translation_audio": (
                        f"/api/audio/{language}/{original['id']}"
                        if translated["audio_url"]
                        else None
                    ),
                }
            )
        return {
            "page": page_number,
            "source_title": source_title,
            "title": translated_title,
            "title_bbox": (
                title_box(pdf_path.resolve(), page_number, source_title)
                if source_title
                else None
            ),
            "language": language,
            "sections": sections,
            "entries": entries,
        }

    @app.get("/api/audio/{language}/{entry_id}")
    def audio(language: str, entry_id: int) -> Response:
        return audio_response(language, entry_id)

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app


app = create_app(Path(os.environ.get("DICTIONARY_PDF", DEFAULT_PDF)))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    uvicorn.run("server:app", host=args.host, port=args.port, reload=args.reload)
