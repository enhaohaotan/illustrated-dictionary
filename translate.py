#!/usr/bin/env python3
"""Translate en.sqlite3 into a complete language-specific SQLite database."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, ValidationError


DEFAULT_MODEL = "gpt-5.6-terra"
DEFAULT_CHUNK_SIZE = 100
TERMINAL_BATCH_STATUSES = {"completed", "failed", "expired", "cancelled"}


class TranslationItem(BaseModel):
    key: str
    text: str
    noun_marker: Optional[str]


class TranslationResult(BaseModel):
    items: list[TranslationItem]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("language", help="target language name")
    parser.add_argument(
        "--code",
        required=True,
        help="language code used for the output filename, for example da or zh",
    )
    parser.add_argument(
        "--source",
        type=Path,
        default=Path("en.sqlite3"),
        help="English source database (default: en.sqlite3)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="translation database (default: <code>.sqlite3)",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="model to use")
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=DEFAULT_CHUNK_SIZE,
        help=f"items per Batch request (default: {DEFAULT_CHUNK_SIZE})",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=30.0,
        help="seconds between Batch status checks (default: 30)",
    )
    parser.add_argument("--force", action="store_true", help="replace output database")
    return parser.parse_args(argv)


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            node.pop("default", None)
            if node.get("type") == "object" or "properties" in node:
                properties = node.get("properties", {})
                node["required"] = list(properties)
                node["additionalProperties"] = False
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(schema)
    return schema


def load_source(source: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        items: list[dict[str, Any]] = []

        for row in connection.execute("SELECT id, title FROM pages ORDER BY id"):
            items.append(
                {
                    "key": f"page:{row['id']}",
                    "kind": "page title",
                    "source_text": row["title"],
                    "context": None,
                }
            )

        for row in connection.execute(
            "SELECT id, number, title FROM sections ORDER BY id"
        ):
            items.append(
                {
                    "key": f"section:{row['id']}",
                    "kind": "section title",
                    "source_text": row["title"],
                    "context": f"Section {row['number']}",
                }
            )

        query = """
            SELECT
                e.id,
                e.source_text,
                e.visual_context,
                e.semantic_meaning,
                p.title AS page_title,
                s.number AS section_number,
                s.title AS section_title
            FROM entries AS e
            JOIN sections AS s ON s.id = e.section_id
            JOIN pages AS p ON p.id = s.page_id
            ORDER BY e.id
        """
        for row in connection.execute(query):
            context = {
                "page_title": row["page_title"],
                "section": f"{row['section_number']} {row['section_title']}",
                "visual_context": row["visual_context"],
                "semantic_meaning": row["semantic_meaning"],
            }
            items.append(
                {
                    "key": f"entry:{row['id']}",
                    "kind": "vocabulary entry",
                    "source_text": row["source_text"],
                    "context": context,
                }
            )
    finally:
        connection.close()

    return items


def instructions(language: str) -> str:
    return f"""Translate English dictionary content into {language}.

Return only translations in the supplied schema. Translate every item exactly once
and copy each key unchanged.

For page and section titles:
- Translate the title naturally and concisely.
- noun_marker must be null.

For page titles, preserve any leading printed unit number exactly, such as "01".

For vocabulary entries:
- Use the supplied context to select the intended sense.
- text must contain only the target-language dictionary headword or concise
  equivalent. Do not include explanations, alternatives, pronunciation, or
  inflection paradigms.
- Preserve the grammatical number and construction of the printed English entry
  whenever the target language naturally does so. In particular, do not silently
  turn a plural entry into a singular dictionary lemma. A genuine cross-language
  number difference is allowed when the natural target-language equivalent uses
  a different grammatical number.
- Follow the normal monolingual dictionary convention of the target language for
  articles, grammatical gender, noun class, and number. Use that language's own
  conventional abbreviations; never copy another language's labels.
- Keep text and noun_marker separate. text contains the translated headword and
  any article that the target language's dictionary convention normally places
  with it. noun_marker contains only a short postposed grammatical label when
  that convention needs one. Never put inflection paradigms in noun_marker.
- If the target language has no relevant noun gender, noun class, or number label
  for an entry, noun_marker must be null.
- Use the concise gender, class, countability, and number notation customary in
  dictionaries of the target language, and use it consistently throughout the
  database. Do not invent labels by translating abbreviations from another
  language.
- All non-noun vocabulary entries have noun_marker null.
- Do not add noun articles to verbs, adjectives, adverbs, sentences, or non-noun
  phrases.

Use one consistent dictionary convention throughout the entire language database.
Do not add facts or fields that were not requested."""


def make_request(
    items: list[dict[str, Any]], language: str, model: str, index: int
) -> dict[str, Any]:
    return {
        "custom_id": f"translation-{index:05d}",
        "method": "POST",
        "url": "/v1/responses",
        "body": {
            "model": model,
            "instructions": instructions(language),
            "input": json.dumps(items, ensure_ascii=False, separators=(",", ":")),
            "max_output_tokens": 12000,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "dictionary_translations",
                    "strict": True,
                    "schema": strict_json_schema(TranslationResult),
                }
            },
        },
    }


def prepare_batch_file(
    directory: Path,
    items: list[dict[str, Any]],
    language: str,
    model: str,
    chunk_size: int,
) -> tuple[Path, dict[str, set[str]]]:
    path = directory / "translations.jsonl"
    expected: dict[str, set[str]] = {}
    with path.open("w", encoding="utf-8") as file:
        for offset in range(0, len(items), chunk_size):
            chunk = items[offset : offset + chunk_size]
            index = offset // chunk_size + 1
            request = make_request(chunk, language, model, index)
            custom_id = request["custom_id"]
            expected[custom_id] = {item["key"] for item in chunk}
            file.write(json.dumps(request, ensure_ascii=False, separators=(",", ":")))
            file.write("\n")
    return path, expected


def response_output_text(body: dict[str, Any]) -> str:
    if body.get("status") == "incomplete":
        raise ValueError(f"response incomplete: {body.get('incomplete_details')}")
    if body.get("error"):
        raise ValueError(f"response error: {body['error']}")

    texts: list[str] = []
    refusals: list[str] = []
    for item in body.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text":
                texts.append(content.get("text", ""))
            elif content.get("type") == "refusal":
                refusals.append(content.get("refusal", "model refusal"))
    if refusals:
        raise ValueError("; ".join(refusals))
    if not texts:
        raise ValueError("response contains no output_text")
    return "".join(texts)


def parse_batch_output(
    jsonl: str, expected: dict[str, set[str]]
) -> tuple[dict[str, TranslationItem], list[str]]:
    translations: dict[str, TranslationItem] = {}
    errors: list[str] = []
    completed_requests: set[str] = set()

    for line in jsonl.splitlines():
        if not line.strip():
            continue
        custom_id = "<unknown>"
        try:
            result = json.loads(line)
            custom_id = result.get("custom_id", "<unknown>")
            expected_keys = expected[custom_id]
            if custom_id in completed_requests:
                raise ValueError("duplicate Batch response")
            if result.get("error"):
                raise ValueError(f"Batch request error: {result['error']}")
            response = result.get("response") or {}
            if response.get("status_code") != 200:
                raise ValueError(
                    f"HTTP {response.get('status_code')}: {response.get('body')}"
                )

            parsed = TranslationResult.model_validate_json(
                response_output_text(response.get("body") or {})
            )
            keys = [item.key for item in parsed.items]
            if len(keys) != len(set(keys)) or set(keys) != expected_keys:
                raise ValueError(
                    f"expected {len(expected_keys)} exact keys, received {len(keys)}"
                )
            for item in parsed.items:
                item.text = item.text.strip()
                if not item.text:
                    raise ValueError(f"empty translation for {item.key}")
                if item.noun_marker is not None:
                    item.noun_marker = item.noun_marker.strip() or None
                if item.key in translations:
                    raise ValueError(f"duplicate translation for {item.key}")
                translations[item.key] = item
            completed_requests.add(custom_id)
        except (KeyError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            errors.append(f"{custom_id}: {exc}")

    missing_requests = set(expected) - completed_requests
    if missing_requests:
        errors.append(f"missing Batch responses: {sorted(missing_requests)}")
    return translations, errors


def wait_for_batch(
    client: OpenAI,
    batch_id: str,
    poll_interval: float,
) -> Any:
    previous_status = None
    while True:
        batch = client.batches.retrieve(batch_id)
        if batch.status != previous_status:
            counts = batch.request_counts
            progress = ""
            if counts:
                progress = (
                    f" ({counts.completed} completed, {counts.failed} failed, "
                    f"{counts.total} total)"
                )
            print(f"{batch.id}: {batch.status}{progress}")
            previous_status = batch.status
        if batch.status in TERMINAL_BATCH_STATUSES:
            return batch
        time.sleep(poll_interval)


def create_translation_database(
    destination: Path,
    source: Path,
    translations: dict[str, TranslationItem],
    schema: Path,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    temporary.unlink(missing_ok=True)

    source_connection = sqlite3.connect(f"file:{source.resolve()}?mode=ro", uri=True)
    output_connection = sqlite3.connect(temporary)
    try:
        output_connection.executescript(schema.read_text(encoding="utf-8"))

        output_connection.executemany(
            "INSERT INTO pages (id, title, see_also) VALUES (?, ?, ?)",
            (
                (
                    item_id,
                    translated_page_title(
                        source_title,
                        translations[f"page:{item_id}"].text,
                    ),
                    see_also,
                )
                for item_id, source_title, see_also in source_connection.execute(
                    "SELECT id, title, see_also FROM pages"
                )
            ),
        )
        output_connection.executemany(
            """
            INSERT INTO sections (id, page_id, number, title)
            VALUES (?, ?, ?, ?)
            """,
            (
                (
                    item_id,
                    page_id,
                    number,
                    translations[f"section:{item_id}"].text,
                )
                for item_id, page_id, number in source_connection.execute(
                    "SELECT id, page_id, number FROM sections"
                )
            ),
        )
        output_connection.executemany(
            """
            INSERT INTO entries (
                id,
                section_id,
                number,
                source_text,
                visual_context,
                semantic_meaning,
                bbox_left,
                bbox_top,
                bbox_right,
                bbox_bottom,
                audio_url,
                noun_marker
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                (
                    item_id,
                    section_id,
                    number,
                    translations[f"entry:{item_id}"].text,
                    visual_context,
                    semantic_meaning,
                    bbox_left,
                    bbox_top,
                    bbox_right,
                    bbox_bottom,
                    None,
                    translations[f"entry:{item_id}"].noun_marker,
                )
                for (
                    item_id,
                    section_id,
                    number,
                    visual_context,
                    semantic_meaning,
                    bbox_left,
                    bbox_top,
                    bbox_right,
                    bbox_bottom,
                ) in source_connection.execute(
                    """
                    SELECT
                        id,
                        section_id,
                        number,
                        visual_context,
                        semantic_meaning,
                        bbox_left,
                        bbox_top,
                        bbox_right,
                        bbox_bottom
                    FROM entries
                    """
                )
            ),
        )
        violations = output_connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ValueError(f"foreign key violations: {violations}")
        output_connection.commit()
    except BaseException:
        output_connection.close()
        source_connection.close()
        temporary.unlink(missing_ok=True)
        raise
    else:
        output_connection.close()
        source_connection.close()
        temporary.replace(destination)


def translated_page_title(source: str, translated: str) -> str:
    source_match = re.match(r"^(\d+)\s+", source)
    if not source_match:
        return translated
    number = source_match.group(1)
    text = re.sub(r"^\d+\s+", "", translated).strip()
    if not text:
        raise ValueError(f"empty translated page title after number {number}")
    return f"{number} {text}"


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(__file__).with_name(".env"), override=False)
    args = parse_args(argv)
    if not re.fullmatch(r"[a-z]{2,3}(?:-[A-Za-z0-9]+)*", args.code):
        print("error: --code must look like da, zh, or pt-BR", file=sys.stderr)
        return 2
    if not args.source.is_file():
        print(f"error: source database not found: {args.source}", file=sys.stderr)
        return 2
    if args.chunk_size < 1 or args.poll_interval < 0:
        print(
            "error: --chunk-size must be >= 1 and --poll-interval must be >= 0",
            file=sys.stderr,
        )
        return 2
    if not os.environ.get("OPENAI_API_KEY"):
        print("error: OPENAI_API_KEY is not set in .env or the shell", file=sys.stderr)
        return 2

    destination = args.output or Path(f"{args.code}.sqlite3")
    if destination.resolve() == args.source.resolve():
        print("error: translation output cannot replace the source database", file=sys.stderr)
        return 2
    if destination.exists() and not args.force:
        print(
            f"error: output already exists: {destination} (use --force to replace it)",
            file=sys.stderr,
        )
        return 2

    try:
        items = load_source(args.source)
    except sqlite3.Error as exc:
        print(f"error: cannot read source database: {exc}", file=sys.stderr)
        return 1

    print(
        f"Preparing {len(items)} items from {args.source} "
        f"for translation into {args.language}..."
    )
    client = OpenAI()
    with tempfile.TemporaryDirectory(prefix="dictionary-translation-") as temp_dir:
        batch_file, expected = prepare_batch_file(
            Path(temp_dir),
            items,
            args.language,
            args.model,
            args.chunk_size,
        )
        print(f"Prepared {len(expected)} Batch requests")
        with batch_file.open("rb") as stream:
            uploaded = client.files.create(file=stream, purpose="batch")
        batch = client.batches.create(
            input_file_id=uploaded.id,
            endpoint="/v1/responses",
            completion_window="24h",
            metadata={
                "description": f"dictionary translation: {args.code}",
                "language": args.code,
            },
        )
        print(f"Submitted {batch.id}")
        batch = wait_for_batch(client, batch.id, args.poll_interval)

        errors: list[str] = []
        translations: dict[str, TranslationItem] = {}
        if batch.output_file_id:
            output = client.files.content(batch.output_file_id).text
            translations, errors = parse_batch_output(output, expected)
        else:
            errors.append("Batch produced no output file")
        if batch.error_file_id:
            error_text = client.files.content(batch.error_file_id).text.strip()
            if error_text:
                errors.append(f"Batch error file:\n{error_text}")
        if batch.status != "completed":
            errors.append(f"Batch ended with status {batch.status}")

    if errors:
        print("error: translation Batch was incomplete:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1

    expected_keys = {item["key"] for item in items}
    if set(translations) != expected_keys:
        print("error: translation set does not match source IDs", file=sys.stderr)
        return 1

    schema = Path(__file__).with_name("schema.sql")
    try:
        create_translation_database(destination, args.source, translations, schema)
    except (KeyError, OSError, ValueError, sqlite3.Error) as exc:
        print(f"error: cannot create translation database: {exc}", file=sys.stderr)
        return 1

    counts = {prefix: 0 for prefix in ("page", "section", "entry")}
    for key in translations:
        counts[key.split(":", 1)[0]] += 1
    print(
        f"Created {destination}: {counts['page']} pages, "
        f"{counts['section']} sections, {counts['entry']} entries"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
