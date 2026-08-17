# Illustrated dictionary extractor

A small Python CLI that renders selected PDF pages and uses the OpenAI Batch API
to extract printed vocabulary plus its visual context. Consecutive pages are sent
to the model together, while the structured JSON result keeps entries assigned to
the PDF page on which their source text is printed.

## Setup

Python 3.9 or newer is supported. Install the dependencies in a virtual
environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Then edit `.env` and add your API key:

```dotenv
OPENAI_API_KEY=sk-your-api-key
```

The script loads this file automatically. A shell-level `OPENAI_API_KEY`, when set,
takes precedence over the value in `.env`. The `.env` file is ignored by Git.
Use `.env.example` as a clean template if needed.

The selected model must support image input at `original` detail, Structured Outputs,
and the Responses endpoint in Batch. The default is `gpt-5.6-terra`.

## Usage

```bash
python extract.py vocabulary-book.pdf --start 10 --end 25
```

Useful options:

```bash
python extract.py book.pdf --start 10 --end 13 --model gpt-5.6-terra
python extract.py book.pdf --start 10 --end 13 --output-dir results
python extract.py book.pdf --start 10 --end 13 --force
```

Page numbers are 1-based and inclusive. The first example processes `10+11`, then
`12+13`, and writes files such as `output/pages_010_011.json`. An unpaired final
page is processed alone. Existing pair outputs are skipped unless `--force` is
given.

Pages are rendered to lossless 300-DPI PNGs and labeled individually inside each
request. Every request contains one page pair (or the final single page), and all
pending requests are submitted through the Batch API. Large runs are automatically
split across Batch input files. The command waits for the batches to finish; OpenAI
documents a completion window of up to 24 hours.

Run `python extract.py --help` for all options.

## Build the English database

Build `en.sqlite3` from the completed files in `output/`:

```bash
python build_db.py
```

The database contains the `pages`, `sections`, and `entries` tables defined in
`schema.sql`. Sections that span two PDF pages are stored once per physical page,
so every entry remains associated with the page on which it is printed. English
entries have `NULL` in the `forms` column.

## Translate the database

Create one complete SQLite database per target language with the OpenAI Batch API:

```bash
python translate.py Danish --code da
python translate.py Chinese --code zh
```

The default model is `gpt-5.6-terra`; use `--model` to select another compatible
model. Existing databases are not replaced unless `--force` is supplied. The API
key is loaded from the same `.env` file used by `extract.py`.

Every language database uses the same `schema.sql` as `en.sqlite3` and is fully
self-contained. Page titles, section titles, and all 10,359 entry `source_text`
values are translated. Page relationships, printed numbers, See also references,
visual context, and semantic meaning are copied unchanged into the language database.

Dictionary-style inflection notation is stored separately in `forms`. For example,
Danish stores `hus` in `source_text` and `-et, -e, -ene` in `forms`. Chinese stores
only the translated word in `source_text`, leaving `forms` as `NULL`. Text-to-speech
can therefore read `source_text` directly without parsing dictionary notation.
