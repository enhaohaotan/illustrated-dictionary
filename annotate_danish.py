#!/usr/bin/env python3
"""Annotate Danish noun headwords with gender/number markers from COR data."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


DEFAULT_COR = Path("/tmp/cor1.5.1.0.tsv")
DEFAULT_COR_EXT = Path("/tmp/corext1.0.tsv")
ARTICLE_RE = re.compile(r"^(en|et)\s+(.+)$", re.IGNORECASE)

# COR deliberately records morphology rather than sense-specific countability.
# These entries have no recorded plural in COR but are countable in the book's
# illustrated sense, so their en/et article must be retained.
COUNTABLE_WITHOUT_COR_PLURAL = {
    84, 187, 668, 1017, 1028, 1481, 1483, 1824, 2362, 3240, 3241,
    3971, 4418, 4561, 4569, 6070, 6214, 6242, 6417, 6419, 7591,
    7602, 7914, 7923, 7958, 7988, 7992, 8981, 8983, 8984, 8986,
    9915, 9916, 9917, 9918, 9919,
}

# Context disambiguates these as mass nouns or fields even though COR also
# records countable senses or plural forms for the same lemma.
CONTEXTUAL_MASS = {
    3444: "fk.",   # frozen yogurt as a food substance
    1469: "itk.",  # salt on the dining table
    1470: "itk.",  # ground pepper
    2675: "itk.",  # confectionery
    4000: "fk.",   # mathematics as a school subject
    4007: "itk.",  # the English language
    4363: "itk.",  # iodine
    4369: "itk.",  # lead
    4483: "fk.",   # humanities
    4484: "fk.",   # politics / political science
    4485: "fk.",   # literature as a field
    4487: "fk.",   # economics
    4488: "fk.",   # philosophy
    4489: "fk.",   # history as a field
    4490: "fk.",   # social science as a field
    4491: "fk.",   # sociology
    4492: "fk.",   # law as a field
    4493: "fk.",   # medicine as a field
    4494: "fk.",   # nursing as a field
    4510: "fk.",   # chemistry
    4511: "fk.",   # physics
    4512: "fk.",   # biology
    4513: "fk.",   # engineering science
    4514: "fk.",   # natural science as a field
    4515: "fk.",   # zoology
    5104: "fk.",   # the arts
    5143: "itk.",  # sales as a business function
    5145: "fk.",   # public relations
    6899: "fk.",   # paragliding as an activity
    7083: "fk.",   # blues as a music genre
    7318: "itk.",  # sand on a beach
    8804: "itk.",  # the French language
    8816: "itk.",  # the German language
    8819: "itk.",  # the Danish language
    8822: "itk.",  # the Norwegian language
    8825: "itk.",  # the Swedish language
    8828: "itk.",  # the Finnish language
    8906: "itk.",  # the Greek language
}

# These plural readings share their spelling with another part of speech or a
# singular form, so the English entry is used to resolve them explicitly.
CONTEXTUAL_PLURAL = {
    240, 260, 673, 1253, 2698, 2723, 3762, 3771, 5327, 6167,
    6241, 6294, 6652, 6656, 6853, 6971,
}

# These new compounds/variant spellings happen to equal a different COR
# lemma's plural form, but the illustrated entry itself is singular.
ARTICLE_PLURAL_FALSE_POSITIVES = {4655, 6702}

# English plural-looking headwords whose normal Danish equivalent is singular.
# These are lexical differences, not annotation errors.
SINGULAR_DANISH_FOR_ENGLISH_PLURAL = {
    "binoculars",
    "clippers",
    "crossroads",
    "customs",
    "headquarters",
    "overalls",
    "pajamas",
    "pliers",
    "pyjamas",
    "savings",
    "scissors",
    "shears",
    "tongs",
    "tweezers",
}
SINGULAR_ENGLISH_S_WORDS = {
    "atlas",
    "bagpipes",
    "biceps",
    "canvas",
    "christmas",
    "cms",
    "equals",
    "gas",
    "gps",
    "headquarters",
    "lens",
    "maths",
    "means",
    "news",
    "pancreas",
    "quadriceps",
    "rhinoceros",
    "series",
    "species",
    "summons",
    "texas",
    "triceps",
    "triceratops",
}
IRREGULAR_ENGLISH_PLURALS = {
    "children",
    "dice",
    "feet",
    "geese",
    "men",
    "mice",
    "people",
    "teeth",
    "women",
}

# Context-specific lexical number differences that cannot be decided from the
# English headword alone.
KEEP_DANISH_SINGULAR = {
    719,   # overalls -> kedeldragt
    734,   # scrubs -> operationsdragt
    766,   # pajamas -> pyjamas
    874,   # string of pearls -> perlekæde
    1107,  # scales -> vægt
    1150,  # hospital notes -> patientjournal
    1154,  # scrubs -> arbejdsdragt
    1252,  # opera glasses -> teaterkikkert
    1665,  # kitchen scales -> køkkenvægt
    1760,  # bathroom scales -> badevægt
    2018,  # needle-nose pliers -> spidstang
    2022,  # bull-nose pliers -> fladtang
    2239,  # long-handled shears -> grensaks
    2275,  # secateurs -> beskæresaks
    2411,  # crossroads -> vejkryds
    2490,  # savings -> opsparing
    2516,  # scales -> vægt
    4181,  # scales -> vægt
    4184,  # tongs -> tang
    4538,  # headquarters -> hovedkontor
    4629,  # minutes -> referat
    5364,  # roadworks -> vejarbejde
    5578,  # leathers -> læderdragt
    5825,  # customs -> told
    7481,  # TV series -> sæson
    7490,  # commercial break / adverts -> reklamepause
    7539,  # contents -> indholdsfortegnelse
    7871,  # binoculars -> kikkert
    9564,  # herd of cows -> kvæghjord
    9719,  # plant name: fairy elephant's feet
    9824,  # mushroom name: chicken of the woods
    10027, # scales -> vægt
    10050, # gallon definition contains plural unit text
    240,   # boyfriend and girlfriend -> kærestepar
}

PLURAL_TRANSLATION_OVERRIDES: dict[int, tuple[str, str]] = {
    928: ("peep-toes", "fk. pl."),
    951: ("mokasiner", "fk. pl."),
    953: ("skolæste", "fk. pl."),
    969: ("højskaftede sneakers", "fk. pl."),
    1443: ("bøger", "fk. pl."),
    1600: ("målebægre", "itk. pl."),
    2265: ("bindestrips", "fk. pl."),
    2818: ("udrykningslys", "itk. pl."),
    3159: ("avocadoer", "fk. pl."),
    3318: ("jalapeñoer", "fk. pl."),
    3562: ("fødselsdagslys", "itk. pl."),
    3587: ("kapers", "fk. pl."),
    3947: ("grøntsagsstænger", "fk. pl."),
    4637: ("papirclips", "fk. pl."),
    4873: ("yams", "fk. pl."),
    5305: ("handlingspunkter", "itk. pl."),
    7021: ("maracas", "fk. pl."),
    7067: ("scenelys", "itk. pl."),
    7659: ("terninger", "fk. pl."),
    7862: ("lys", "itk. pl."),
    7898: ("stjernebilleder i dyrekredsen", "itk. pl."),
    8324: ("mauretanere", "fk. pl."),
    8327: ("kapverdeanere", "fk. pl."),
    8333: ("gambiere", "fk. pl."),
    8345: ("liberianere", "fk. pl."),
    8375: ("beninesere", "fk. pl."),
    8381: ("sãotoméere", "fk. pl."),
    8393: ("egyptere", "fk. pl."),
    8396: ("sudanesere", "fk. pl."),
    8399: ("sydsudanesere", "fk. pl."),
    8405: ("djiboutianere", "fk. pl."),
    8426: ("kongolesere", "fk. pl."),
    8429: ("kongolesere", "fk. pl."),
    8441: ("mozambicanere", "fk. pl."),
    8474: ("madagaskarere", "fk. pl."),
    8495: ("peruanere", "fk. pl."),
    8516: ("paraguayanere", "fk. pl."),
    8519: ("uruguayanere", "fk. pl."),
    8559: ("cubanere", "fk. pl."),
    8571: ("dominikanere", "fk. pl."),
    8577: ("trinidadiere eller tobagonere", "fk. pl."),
    8586: ("dominicanere", "fk. pl."),
    8589: ("lucianere", "fk. pl."),
    8592: ("vincentianere", "fk. pl."),
    8613: ("palauanere", "fk. pl."),
    8634: ("vanuatuanere", "fk. pl."),
    8643: ("asiater", "fk. pl."),
    8646: ("tyrkere", "fk. pl."),
    8700: ("kasakher", "fk. pl."),
    8703: ("usbekere", "fk. pl."),
    8754: ("burmesere", "fk. pl."),
    8766: ("cambodjanere", "fk. pl."),
    8796: ("portugisere", "fk. pl."),
    8814: ("luxembourgere", "fk. pl."),
    8844: ("tjekker", "fk. pl."),
    8871: ("kroater", "fk. pl."),
    8901: ("nordmakedonere", "fk. pl."),
    8919: ("tyrkere", "fk. pl."),
}

REMOVE_ARTICLE_WITHOUT_MARKER = {7690}  # dam, the board game

# Multiword countable headwords missed by the original single-word noun pass.
SINGULAR_PHRASE_OVERRIDES: dict[int, str] = {
    110: "en balde / den store sædemuskel",
    176: "et hormonsystem",
    234: "et indgiftet familiemedlem",
    242: "en enlig forsørger",
    655: "et barberet hoved",
    841: "en snorkel og en maske",
    1277: "en afbalanceret kost",
    1278: "en kaloriekontrolleret kost",
    1587: "en morter og en støder",
    1848: "en ampere",
    2068: "et forlængerskaft til malerrulle",
    2652: "et stort udvalg",
    2893: "en varm chokolade",
    3379: "en sød chilisauce",
    3400: "en fast ost",
    3401: "en halvfast ost",
    3402: "en halvblød ost",
    3403: "en blød ost",
    3416: "et kogt æg",
    3419: "et pocheret æg",
    3629: "en dobbelt espresso",
    3633: "en kaffe med mælk",
    3634: "en flat white",
    3645: "en appelsinjuice med frugtkød",
    3646: "en appelsinjuice uden frugtkød",
    3648: "en ananasjuice",
    3650: "en mangojuice",
    3651: "en tranebærjuice",
    3659: "en Irish coffee",
    3663: "en sort kaffe",
    3677: "en hvid te",
    3680: "en te med citron",
    3682: "en sort te",
    3683: "en grøn te",
    3684: "en kamille-te",
    3687: "en te med mælk",
    3741: "en rom og cola",
    3743: "en vodka og appelsinjuice",
    3744: "en gin og tonic",
    3751: "en whisky med vand",
    4808: "et røveri / et indbrud",
    4955: "et militært transportfly",
    5212: "en god lytter",
    5241: "en professionel indstilling",
    5257: "en digital tegnebog",
    5258: "en digital valuta",
    5291: "en økonomisk nedgang",
    5293: "en finansiel rådgiver",
    5922: "en venstre cornerback",
    5924: "en venstre defensive end",
    5925: "en venstre safety",
    5926: "en venstre defensive tackle",
    5927: "en middle linebacker",
    5928: "en højre defensive tackle",
    5929: "en højre safety",
    5930: "en højre defensive end",
    5932: "en højre cornerback",
    5933: "en wide receiver",
    5934: "en højre tackle",
    5935: "en højre guard",
    5936: "en running back",
    5940: "en venstre guard",
    5941: "en venstre tackle",
    5942: "en wide receiver",
    5943: "en wide receiver",
    5948: "en målzone",
    6197: "en center",
    6299: "en forhånd",
    6300: "en baghånd",
    5949: "en neutral zone",
    6141: "en third man",
    6148: "en return crease",
    6152: "en popping crease",
    6155: "en square leg",
    6157: "en bowling crease",
    6161: "en fine leg",
    6776: "en pole position",
    6965: "en romantisk komedie",
    6968: "et historisk drama",
    7057: "en lavere tonehøjde",
    7058: "en højere tonehøjde",
    7345: "en spand og en skovl",
    7818: "en hægte og en malle",
    8931: "en let regnbyge",
    9096: "en kæbeløs fisk",
    9105: "en tyrannosaurus rex",
    9481: "en britisk korthår",
    9483: "en maine coon",
    9485: "en eksotisk korthår",
    9488: "en burmeser",
    9492: "en japansk bobtail",
    9494: "en abyssinier",
    9495: "en amerikansk curl",
    9517: "en shih tzu",
}

# Multiword mass and plural nouns do not take en/et, but still need the same
# compact postposed marker as their single-word counterparts.
PHRASE_MARKER_OVERRIDES: dict[int, str] = {
    647: "itk.", 648: "itk.", 649: "itk.", 661: "itk.",
    662: "itk.", 663: "itk.", 664: "itk.", 677: "itk.",
    678: "itk.", 679: "itk.", 683: "itk.", 684: "itk.",
    685: "itk.", 686: "itk.", 687: "itk.", 688: "itk.",
    1247: "fk.", 1268: "itk.", 1269: "itk.", 1281: "fk. pl.",
    1284: "fk.", 1356: "fk. pl.", 1718: "itk.", 1719: "itk.",
    1888: "itk.", 1889: "itk.", 1943: "itk.", 2678: "itk.",
    2842: "itk. pl.", 2862: "fk.", 2952: "itk.", 2953: "itk.",
    2954: "itk.", 2955: "itk.", 2978: "itk.", 2979: "itk.",
    2980: "itk.", 2981: "itk.", 3040: "fk. pl.", 3041: "fk. pl.",
    3099: "fk. pl.", 3367: "fk.", 3369: "fk.", 3373: "fk.",
    3387: "fk.", 3395: "itk. pl.", 3396: "fk. pl.", 3408: "fk.",
    3457: "fk.", 3477: "itk.", 3478: "itk.", 3480: "itk.",
    3481: "itk.", 3484: "fk.", 3486: "fk. pl.", 3487: "itk.",
    3548: "fk. pl.", 3567: "itk.", 3572: "itk.", 3574: "itk.",
    3583: "itk. pl.", 3584: "fk. pl.", 3585: "fk. pl.",
    3586: "fk. pl.", 3593: "itk.", 3603: "fk.", 3604: "fk.",
    3605: "fk.", 3606: "fk.", 3610: "fk.", 3692: "itk.",
    3780: "fk. pl.", 3880: "fk. pl.", 3882: "fk.", 3903: "fk.",
    3920: "fk. pl.", 3924: "fk. pl.", 3941: "fk. pl.",
    3959: "fk. pl.", 4139: "itk.", 4329: "itk. pl.", 4673: "fk.",
    5095: "fk. pl.", 5231: "fk.", 5233: "fk.", 5254: "pl.",
    5325: "fk. pl.", 5944: "fk. pl.", 5955: "pl.",
    5970: "fk. pl.", 6055: "fk.", 6201: "fk. pl.",
    6321: "fk. pl.", 6324: "fk. pl.", 6850: "fk. pl.",
    6934: "itk.", 7098: "fk.", 7216: "itk. pl.",
    7217: "itk. pl.", 7364: "fk. pl.", 7432: "fk. pl.",
    7595: "fk.", 7866: "fk. pl.", 8987: "fk. pl.",
    8998: "fk. pl.", 9004: "fk.", 9006: "fk. pl.", 9007: "fk.",
    9008: "fk.", 9014: "itk. pl.", 9019: "fk. pl.",
    9020: "fk. pl.", 9028: "fk. pl.", 9239: "itk. pl.",
    9346: "fk. pl.", 9357: "fk. pl.", 9819: "fk. pl.",
    9899: "fk. pl.", 9942: "itk. pl.", 9944: "itk.",
    9949: "fk. pl.", 10111: "itk. pl.", 10112: "itk. pl.",
    10114: "fk. pl.",
}


@dataclass(frozen=True)
class Form:
    lemma_id: str
    grammar: str
    text: str

    @property
    def gender(self) -> str | None:
        if ".fk." in self.grammar:
            return "fk"
        if ".itk." in self.grammar:
            return "itk"
        return None

    @property
    def singular(self) -> bool:
        return ".sg.ubest" in self.grammar

    @property
    def plural(self) -> bool:
        return ".pl.ubest" in self.grammar or self.grammar == "sb.pl"


@dataclass
class CorIndex:
    forms: dict[str, list[Form]]
    parts_of_speech: dict[str, set[str]]
    lemma_grammar: dict[str, set[str]]
    lemma_forms: dict[str, list[Form]]
    adjective_forms: dict[str, list[Form]]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path("da.sqlite3"))
    parser.add_argument("--english-database", type=Path, default=Path("en.sqlite3"))
    parser.add_argument("--cor", type=Path, default=DEFAULT_COR)
    parser.add_argument("--cor-ext", type=Path, default=DEFAULT_COR_EXT)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("danish_noun_annotation_report.json"),
    )
    parser.add_argument("--apply", action="store_true", help="write changes to the database")
    return parser.parse_args()


def cor_rows(path: Path, extended: bool) -> Iterable[tuple[str, str, str]]:
    with path.open(encoding="utf-8", newline="") as stream:
        for row in csv.reader(stream, delimiter="\t"):
            if extended:
                if len(row) < 8:
                    continue
                form_id, grammar, text = row[0], row[6], row[7]
                lemma_id = ".".join(form_id.split(".")[:3])
            else:
                if len(row) < 5:
                    continue
                form_id, grammar, text = row[0], row[3], row[4]
                lemma_id = ".".join(form_id.split(".")[:2])
            yield lemma_id, grammar, text


def load_cor(cor: Path, cor_ext: Path) -> CorIndex:
    for path in (cor, cor_ext):
        if not path.is_file():
            raise SystemExit(f"Missing COR data: {path}")

    forms: dict[str, list[Form]] = defaultdict(list)
    parts_of_speech: dict[str, set[str]] = defaultdict(set)
    lemma_grammar: dict[str, set[str]] = defaultdict(set)
    lemma_forms: dict[str, list[Form]] = defaultdict(list)
    adjective_forms: dict[str, list[Form]] = defaultdict(list)
    for path, extended in ((cor, False), (cor_ext, True)):
        for lemma_id, grammar, text in cor_rows(path, extended):
            key = text.casefold()
            parts_of_speech[key].add(grammar.split(".", 1)[0])
            form = Form(lemma_id, grammar, text)
            lemma_forms[lemma_id].append(form)
            if grammar == "sb" or grammar.startswith("sb."):
                forms[key].append(form)
                lemma_grammar[lemma_id].add(grammar)
            elif grammar == "adj" or grammar.startswith("adj."):
                adjective_forms[key].append(form)
    return CorIndex(
        dict(forms),
        dict(parts_of_speech),
        dict(lemma_grammar),
        dict(lemma_forms),
        dict(adjective_forms),
    )


def marker(genders: set[str], plural: bool) -> str:
    if genders == {"fk"}:
        return "fk. pl." if plural else "fk."
    if genders == {"itk"}:
        return "itk. pl." if plural else "itk."
    return "pl." if plural else ""


def english_headword(text: str) -> str | None:
    first_variant = text.replace("\n", " ").split("/", 1)[0]
    first_variant = re.sub(r"\([^)]*\)", " ", first_variant)
    words = re.findall(r"[^\W\d_]+(?:['’][^\W\d_]+)?", first_variant, re.UNICODE)
    if not words:
        return None
    lowered = [word.casefold() for word in words]
    if "of" in lowered:
        position = lowered.index("of")
        if position:
            return lowered[position - 1]
    return lowered[-1]


def is_english_plural(text: str) -> bool:
    headword = english_headword(text)
    if not headword or headword.endswith(("'s", "’s")):
        return False
    if headword in SINGULAR_ENGLISH_S_WORDS:
        return False
    if headword in IRREGULAR_ENGLISH_PLURALS:
        return True
    if headword.endswith(("ss", "us", "is", "ics", "ness")):
        return False
    return headword.endswith("s")


def match_case(original: str, replacement: str) -> str:
    if original.isupper():
        return replacement.upper()
    if original[:1].isupper():
        return replacement[:1].upper() + replacement[1:]
    return replacement


def target_form(index: CorIndex, form: Form, grammar: str) -> str | None:
    values = {
        candidate.text
        for candidate in index.lemma_forms.get(form.lemma_id, [])
        if candidate.grammar == grammar
    }
    return next(iter(values)) if len(values) == 1 else None


def pluralize_adjectives(index: CorIndex, text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        word = match.group(0)
        candidates = [
            form
            for form in index.adjective_forms.get(word.casefold(), [])
            if form.grammar.startswith("adj.sg.ubest")
        ]
        plurals = {
            value
            for form in candidates
            if (value := target_form(index, form, "adj.pl")) is not None
        }
        if len(plurals) != 1:
            return word
        return match_case(word, next(iter(plurals)))

    return re.sub(r"[^\W\d_]+", replace, text, flags=re.UNICODE)


def pluralize_danish(index: CorIndex, bare: str, article: str) -> str | None:
    gender = "fk" if article == "en" else "itk"
    starts = list(range(len(bare)))
    for start in starts:
        noun = bare[start:]
        candidates = [
            form
            for form in index.forms.get(noun.casefold(), [])
            if form.singular and form.gender == gender
        ]
        plurals = {
            value
            for form in candidates
            if (value := target_form(
                index, form, f"sb.{gender}.pl.ubest"
            )) is not None
        }
        if len(plurals) != 1:
            continue
        plural_noun = match_case(noun, next(iter(plurals)))
        prefix = pluralize_adjectives(index, bare[:start])
        return prefix + plural_noun
    return None


def strip_article(text: str) -> tuple[str | None, str]:
    match = ARTICLE_RE.fullmatch(text.strip())
    if not match:
        return None, text.strip()
    return match.group(1).lower(), match.group(2).strip()


def noun_forms(index: CorIndex, text: str) -> tuple[list[Form], list[Form]]:
    candidates = index.forms.get(text.casefold(), [])
    return (
        [candidate for candidate in candidates if candidate.singular],
        [candidate for candidate in candidates if candidate.plural],
    )


def has_recorded_plural(index: CorIndex, form: Form) -> bool:
    return any(".pl" in value for value in index.lemma_grammar[form.lemma_id])


def annotate_entry(
    entry_id: int,
    text: str,
    index: CorIndex,
    english_text: str | None = None,
    current_marker: str | None = None,
) -> tuple[str, str | None, str | None]:
    article, bare = strip_article(text)
    singular, plural = noun_forms(index, bare)

    if entry_id in SINGULAR_PHRASE_OVERRIDES:
        return (
            SINGULAR_PHRASE_OVERRIDES[entry_id],
            None,
            "countable multiword noun with missing article",
        )

    if entry_id in PHRASE_MARKER_OVERRIDES:
        return (
            text,
            PHRASE_MARKER_OVERRIDES[entry_id],
            "multiword mass or plural noun with missing marker",
        )

    if entry_id in PLURAL_TRANSLATION_OVERRIDES:
        plural_text, plural_marker = PLURAL_TRANSLATION_OVERRIDES[entry_id]
        return plural_text, plural_marker, "context-confirmed plural translation"

    if entry_id in REMOVE_ARTICLE_WITHOUT_MARKER:
        return bare, None, "article removed from noun without gender"

    if entry_id in CONTEXTUAL_MASS:
        return bare, CONTEXTUAL_MASS[entry_id], "contextual mass noun"

    if article:
        article_gender = "fk" if article == "en" else "itk"

        english_head = english_headword(english_text or "")
        if (
            english_text
            and is_english_plural(english_text)
            and english_head not in SINGULAR_DANISH_FOR_ENGLISH_PLURAL
            and entry_id not in KEEP_DANISH_SINGULAR
        ):
            plural_text = pluralize_danish(index, bare, article)
            if plural_text is not None:
                return (
                    plural_text,
                    f"{article_gender}. pl.",
                    "English plural with Danish singular article",
                )

        matching_singular = [form for form in singular if form.gender == article_gender]
        if (
            matching_singular
            and entry_id not in COUNTABLE_WITHOUT_COR_PLURAL
            and all(not has_recorded_plural(index, form) for form in matching_singular)
        ):
            return bare, f"{article_gender}.", "COR noun without plural; context reviewed"

        if (
            not singular
            and plural
            and entry_id not in ARTICLE_PLURAL_FALSE_POSITIVES
        ):
            genders = {form.gender for form in plural if form.gender}
            return bare, marker(genders, plural=True), "plural form with incorrect article"

        singular_genders = {form.gender for form in singular if form.gender}
        if len(singular_genders) == 1:
            expected = "en" if singular_genders == {"fk"} else "et"
            if expected != article:
                return f"{expected} {bare}", None, "COR gender correction"
        if current_marker is not None:
            return text, None, "article already expresses noun gender"
        return text, None, None

    if entry_id in CONTEXTUAL_PLURAL:
        all_forms = singular + plural
        genders = {form.gender for form in all_forms if form.gender}
        return bare, marker(genders, plural=True), "context-confirmed plural noun"

    if plural and not singular and index.parts_of_speech.get(bare.casefold()) == {"sb"}:
        genders = {form.gender for form in plural if form.gender}
        plural_marker = marker(genders, plural=True)
        if plural_marker == "pl." and current_marker in {"fk. pl.", "itk. pl."}:
            plural_marker = current_marker
        return bare, plural_marker, "unambiguous COR plural noun"

    return text, None, None


def main() -> None:
    args = parse_args()
    if not args.database.is_file():
        raise SystemExit(f"Database not found: {args.database}")
    if not args.english_database.is_file():
        raise SystemExit(f"English database not found: {args.english_database}")
    index = load_cor(args.cor, args.cor_ext)

    english_connection = sqlite3.connect(args.english_database)
    try:
        english_texts = dict(
            english_connection.execute("SELECT id, source_text FROM entries")
        )
    finally:
        english_connection.close()

    connection = sqlite3.connect(args.database)
    connection.row_factory = sqlite3.Row
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(entries)").fetchall()
        }
        if "noun_marker" not in columns:
            raise SystemExit("entries.noun_marker is missing")

        changes = []
        reasons: Counter[str] = Counter()
        rows = connection.execute(
            "SELECT id, source_text, noun_marker FROM entries ORDER BY id"
        ).fetchall()
        for row in rows:
            new_text, new_marker, reason = annotate_entry(
                row["id"],
                row["source_text"],
                index,
                english_texts.get(row["id"]),
                row["noun_marker"],
            )
            if reason is None:
                continue
            if new_text == row["source_text"] and new_marker == row["noun_marker"]:
                continue
            changes.append(
                {
                    "id": row["id"],
                    "before": row["source_text"],
                    "after": new_text,
                    "noun_marker": new_marker,
                    "reason": reason,
                }
            )
            reasons[reason] += 1

        if args.apply:
            connection.execute("BEGIN")
            connection.executemany(
                "UPDATE entries SET source_text = ?, noun_marker = ? WHERE id = ?",
                (
                    (change["after"], change["noun_marker"], change["id"])
                    for change in changes
                ),
            )
            connection.commit()
    finally:
        connection.close()

    report = {
        "applied": args.apply,
        "database": str(args.database),
        "sources": {
            "COR": "https://ordregister.dk/files/cor1.5.1.0.tsv",
            "COR_EXT": "https://ordregister.dk/files/corext1.0.tsv",
        },
        "change_count": len(changes),
        "reasons": dict(sorted(reasons.items())),
        "changes": changes,
    }
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    action = "Applied" if args.apply else "Proposed"
    print(f"{action} {len(changes)} changes; report: {args.report}")


if __name__ == "__main__":
    main()
