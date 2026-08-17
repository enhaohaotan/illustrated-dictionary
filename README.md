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
