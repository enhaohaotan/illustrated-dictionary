#!/usr/bin/env python3
"""Extract illustrated vocabulary from selected PDF pages with OpenAI Batch."""

from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Optional

import pymupdf
from openai import OpenAI
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError


DEFAULT_MODEL = "gpt-5.6-terra"
RENDER_DPI = 300
MAX_BATCH_FILE_BYTES = 190 * 1024 * 1024  # API limit is 200 MB.
TERMINAL_BATCH_STATUSES = {"completed", "failed", "expired", "cancelled"}


class VocabularyEntry(BaseModel):
    source_text: str
    number: Optional[str] = None
    section: Optional[str] = None
    visual_context: Optional[str] = None
    semantic_meaning: Optional[str] = None


class PageExtraction(BaseModel):
    page: int
    title: Optional[str] = None
    context: Optional[str] = None
    entries: list[VocabularyEntry]


class SpreadExtraction(BaseModel):
    pages: list[PageExtraction]


INSTRUCTIONS = """You extract vocabulary from illustrated vocabulary-book pages.

Analyze every supplied PDF page image jointly. Illustrations, arrows, pointers, keys,
and diagrams may cross the gutter or clarify text printed on another supplied page.
Systematically inspect the entire page, including headings, lists, margins, captions,
numbered keys, and labels, and extract every visible vocabulary entry.

For every entry:
- Copy source_text exactly as printed, preserving spelling, capitalization,
  punctuation, diacritics, and apparent errors.
- Do not include a visually separate entry number in source_text. Put that number
  only in the number field. Keep digits in source_text when they are an inseparable
  part of the vocabulary text itself (for example, "24-hour").
- Never translate, normalize, silently correct, complete, or invent source text.
- Record a printed number only when one is visibly attached to the entry.
- Record the applicable printed section or heading when there is one.
- Describe concisely what the associated illustration, arrow, pointer, or diagram
  refers to. You may use visual evidence from either supplied page.
- Add a concise semantic meaning only when it helps disambiguate the source text.

Return exactly one page object for each supplied PDF page, even if it has no entries.
Put each entry under the PDF page on which its source text is physically printed,
not the page containing the object it points to. Use the exact PDF page numbers given
in the labels, in ascending order. Completeness is more important than brevity.
"""


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pdf", type=Path, help="input PDF")
    parser.add_argument("--start", type=int, default=1, help="first PDF page (1-based)")
    parser.add_argument("--end", type=int, help="last PDF page (1-based, inclusive)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help="vision model to use")
    parser.add_argument(
        "--output-dir", type=Path, default=Path("output"), help="JSON output directory"
    )
    parser.add_argument("--force", action="store_true", help="replace existing outputs")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=30.0,
        help="seconds between Batch status checks (default: 30)",
    )
    return parser.parse_args(argv)


def output_path(output_dir: Path, pages: tuple[int, ...]) -> Path:
    suffix = "_".join(f"{page:03d}" for page in pages)
    return output_dir / f"pages_{suffix}.json"


def page_pairs(start: int, end: int) -> list[tuple[int, ...]]:
    pages = list(range(start, end + 1))
    return [tuple(pages[index : index + 2]) for index in range(0, len(pages), 2)]


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """Make Pydantic's schema compatible with strict Structured Outputs."""
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


def render_page_as_data_url(document: pymupdf.Document, page_number: int) -> str:
    page = document.load_page(page_number - 1)
    pixmap = page.get_pixmap(dpi=RENDER_DPI, alpha=False)
    png = pixmap.tobytes("png")
    encoded = base64.b64encode(png).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def make_batch_request(
    document: pymupdf.Document, pages: tuple[int, ...], model: str
) -> dict[str, Any]:
    content: list[dict[str, Any]] = [
        {
            "type": "input_text",
            "text": (
                "Extract all vocabulary from the supplied pages. Images are in PDF "
                "page order and each image is preceded by its exact PDF page label."
            ),
        }
    ]
    for index, page in enumerate(pages):
        content.append(
            {
                "type": "input_text",
                "text": f"PDF PAGE {page} ({'first' if index == 0 else 'second'} image):",
            }
        )
        content.append(
            {
                "type": "input_image",
                "image_url": render_page_as_data_url(document, page),
                "detail": "original",
            }
        )

    custom_id = "pages-" + "-".join(f"{page:03d}" for page in pages)
    return {
        "custom_id": custom_id,
        "method": "POST",
        "url": "/v1/responses",
        "body": {
            "model": model,
            "instructions": INSTRUCTIONS,
            "input": [{"role": "user", "content": content}],
            "max_output_tokens": 12000,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "spread_extraction",
                    "strict": True,
                    "schema": strict_json_schema(SpreadExtraction),
                }
            },
        },
    }


def write_batch_files(
    directory: Path,
    document: pymupdf.Document,
    pairs: Iterable[tuple[int, ...]],
    model: str,
) -> tuple[list[Path], dict[str, tuple[int, ...]]]:
    paths: list[Path] = []
    id_to_pages: dict[str, tuple[int, ...]] = {}
    current_file = None
    current_size = 0

    try:
        for pages in pairs:
            request = make_batch_request(document, pages, model)
            custom_id = request["custom_id"]
            line = (
                json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n"
            ).encode("utf-8")
            if len(line) > MAX_BATCH_FILE_BYTES:
                raise ValueError(
                    f"Rendered pair {pages} is too large for one Batch input file"
                )
            if current_file is None or current_size + len(line) > MAX_BATCH_FILE_BYTES:
                if current_file is not None:
                    current_file.close()
                current_path = directory / f"batch_input_{len(paths) + 1:03d}.jsonl"
                paths.append(current_path)
                current_file = current_path.open("wb")
                current_size = 0
            current_file.write(line)
            current_size += len(line)
            id_to_pages[custom_id] = pages
    finally:
        if current_file is not None:
            current_file.close()

    return paths, id_to_pages


def response_output_text(body: dict[str, Any]) -> str:
    if body.get("status") == "incomplete":
        raise ValueError(f"response incomplete: {body.get('incomplete_details')}")
    if body.get("error"):
        raise ValueError(f"response error: {body['error']}")
    refusals: list[str] = []
    texts: list[str] = []
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


def save_batch_results(
    jsonl: str,
    id_to_pages: dict[str, tuple[int, ...]],
    output_dir: Path,
) -> tuple[set[str], list[str]]:
    saved: set[str] = set()
    errors: list[str] = []
    for line in jsonl.splitlines():
        if not line.strip():
            continue
        custom_id = "<unknown>"
        try:
            result = json.loads(line)
            custom_id = result.get("custom_id", "<unknown>")
            pages = id_to_pages[custom_id]
            if result.get("error"):
                raise ValueError(f"Batch request error: {result['error']}")
            response = result.get("response") or {}
            if response.get("status_code") != 200:
                raise ValueError(
                    f"HTTP {response.get('status_code')}: {response.get('body')}"
                )
            extraction = SpreadExtraction.model_validate_json(
                response_output_text(response.get("body") or {})
            )
            expected_pages = set(pages)
            actual_pages = [page.page for page in extraction.pages]
            if len(actual_pages) != len(expected_pages) or set(actual_pages) != expected_pages:
                raise ValueError(
                    f"expected pages {list(pages)}, received page objects {actual_pages}"
                )
            extraction.pages.sort(key=lambda page: page.page)
            destination = output_path(output_dir, pages)
            temporary = destination.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(extraction.model_dump(), ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            temporary.replace(destination)
            saved.add(custom_id)
            print(f"Saved {destination}")
        except (KeyError, ValueError, ValidationError, json.JSONDecodeError) as exc:
            errors.append(f"{custom_id}: {exc}")
    return saved, errors


def submit_batches(client: OpenAI, batch_files: list[Path]) -> dict[str, str]:
    jobs: dict[str, str] = {}
    for path in batch_files:
        with path.open("rb") as stream:
            uploaded = client.files.create(file=stream, purpose="batch")
        batch = client.batches.create(
            input_file_id=uploaded.id,
            endpoint="/v1/responses",
            completion_window="24h",
            metadata={"description": "illustrated vocabulary extraction"},
        )
        jobs[batch.id] = uploaded.id
        print(f"Submitted {batch.id} ({path.name})")
    return jobs


def collect_batches(
    client: OpenAI,
    jobs: dict[str, str],
    id_to_pages: dict[str, tuple[int, ...]],
    output_dir: Path,
    poll_interval: float,
) -> tuple[set[str], list[str]]:
    pending = set(jobs)
    saved: set[str] = set()
    errors: list[str] = []
    last_status: dict[str, str] = {}

    while pending:
        for batch_id in list(pending):
            batch = client.batches.retrieve(batch_id)
            if last_status.get(batch_id) != batch.status:
                print(f"{batch_id}: {batch.status}")
                last_status[batch_id] = batch.status
            if batch.status not in TERMINAL_BATCH_STATUSES:
                continue
            pending.remove(batch_id)
            if batch.output_file_id:
                output = client.files.content(batch.output_file_id).text
                batch_saved, batch_errors = save_batch_results(
                    output, id_to_pages, output_dir
                )
                saved.update(batch_saved)
                errors.extend(batch_errors)
            if batch.error_file_id:
                error_text = client.files.content(batch.error_file_id).text.strip()
                if error_text:
                    errors.append(f"{batch_id} error file:\n{error_text}")
            if batch.status != "completed":
                errors.append(f"{batch_id} ended with status {batch.status}")
        if pending:
            time.sleep(poll_interval)

    return saved, errors


def main(argv: list[str] | None = None) -> int:
    load_dotenv(Path(__file__).with_name(".env"), override=False)
    args = parse_args(argv)
    if not args.pdf.is_file():
        print(f"error: PDF not found: {args.pdf}", file=sys.stderr)
        return 2
    if args.start < 1 or args.poll_interval < 0:
        print(
            "error: --start must be >= 1 and --poll-interval must be >= 0",
            file=sys.stderr,
        )
        return 2

    try:
        document = pymupdf.open(args.pdf)
    except Exception as exc:
        print(f"error: cannot open PDF: {exc}", file=sys.stderr)
        return 2

    with document:
        page_count = document.page_count
        end = page_count if args.end is None else args.end
        if end < args.start or end > page_count:
            print(
                f"error: page range must satisfy 1 <= start <= end <= {page_count}",
                file=sys.stderr,
            )
            return 2

        args.output_dir.mkdir(parents=True, exist_ok=True)
        all_pairs = page_pairs(args.start, end)
        pending_pairs = [
            pages
            for pages in all_pairs
            if args.force or not output_path(args.output_dir, pages).exists()
        ]
        skipped = len(all_pairs) - len(pending_pairs)
        if skipped:
            print(f"Skipping {skipped} page pair(s) with existing output")
        if not pending_pairs:
            print("Nothing to do")
            return 0
        if not os.environ.get("OPENAI_API_KEY"):
            print("error: OPENAI_API_KEY is not set", file=sys.stderr)
            return 2

        try:
            with tempfile.TemporaryDirectory(prefix="vocab-batch-") as temp_dir:
                batch_files, id_to_pages = write_batch_files(
                    Path(temp_dir), document, pending_pairs, args.model
                )
                client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])
                jobs = submit_batches(client, batch_files)
                saved, errors = collect_batches(
                    client,
                    jobs,
                    id_to_pages,
                    args.output_dir,
                    args.poll_interval,
                )
        except (OSError, ValueError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        except Exception as exc:
            print(f"OpenAI API error: {exc}", file=sys.stderr)
            return 1

    missing = set(id_to_pages) - saved
    if missing:
        errors.append("No valid output for: " + ", ".join(sorted(missing)))
    if errors:
        print("\nErrors:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
