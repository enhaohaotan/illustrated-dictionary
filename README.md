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
entries keep their exact printed headwords in `source_text`. The nullable
`noun_marker` column stores a short noun label only when it cannot be expressed
naturally as part of the headword.

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

For Danish countable nouns, the article is stored directly in `source_text`, for
example `et hus` and `en bil`, and `noun_marker` remains `NULL`. Uncountable nouns omit
the article and use `fk.` or `itk.` in `noun_marker`. Plural forms use `fk. pl.` or
`itk. pl.` when gender is known; genuinely plural-only nouns whose gender cannot be
determined use `pl.`. The reader displays this marker in italics after the headword.
No inflection paradigms are stored, and the marker is not included in audio playback.

### Annotate Danish nouns from COR

`annotate_danish.py` checks Danish noun candidates against the official Det Centrale
Ordregister (COR) data. It keeps `en`/`et` on countable singular headwords and moves
gender/number into `noun_marker` for reviewed mass nouns and plural forms. Running it
without `--apply` only creates a report; add `--apply` to update `da.sqlite3`:

```bash
python annotate_danish.py
python annotate_danish.py --apply
```

By default it reads `cor1.5.1.0.tsv` and `corext1.0.tsv` from `/tmp`. Alternative
locations can be supplied with `--cor` and `--cor-ext`. The source files are available
from [Det Centrale Ordregister](https://ordregister.dk/) under open licenses. The
generated `danish_noun_annotation_report.json` records every changed entry and the
reason for the change. This process is entirely local and does not call the OpenAI API.

## Locate entries in the PDF

The included databases already contain normalized bounding boxes for the printed
English vocabulary labels. To regenerate them after rebuilding the databases, run:

```bash
python locate_entries.py EnglishforEveryoneIllustratedEnglishDictionary.pdf
```

The PDF contains an embedded text layer, so the script reads its exact text geometry
instead of applying lower-accuracy image OCR. Coordinates are stored as `bbox_left`,
`bbox_top`, `bbox_right`, and `bbox_bottom`, each in the range `0` to `1`. The same
coordinates are copied to `en.sqlite3`, `da.sqlite3`, and `zh.sqlite3`, so they remain
aligned at every display size. `audio_url` is language-specific and initially `NULL`.

When a spread-crossing illustration places a label on the adjacent physical page,
the locator also corrects that entry's page association without changing its ID.

## Run the web reader

Start the local backend from the project directory:

```bash
python server.py
```

Then open [http://127.0.0.1:8000](http://127.0.0.1:8000). The reader displays each
two-page spread together (`14+15`, `16+17`, and so on), supports spread navigation,
direct page-number jumps, zooming, and switching between every `*.sqlite3` language
database in the project. Entering either page in a spread opens that complete pair.
It requests only the two visible PDF pages and their translations. Translation labels
automatically move to a nearby free position when they would overlap source text or
another translation, and the layout is recalculated after zooming or resizing.

Every located English label and translation is itself an audio click target. Clicking
the printed source position plays English audio; clicking the translated text plays
the selected language. Clicks remain inactive while `audio_url` is `NULL`. Later, an
`audio_url` may be an HTTP(S) URL or a path relative to the local `audio/` directory;
`/api/audio/{language}/{entry_id}` serves it on demand.
