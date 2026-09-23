"""Deterministic re-implementation of dblp's "standard" BibTeX export.

Renders an entry from the fields of the local SQLite index (see local_index.py)
so that it matches https://dblp.org/rec/<key>.bib byte for byte, except for the
timestamp line (the dump only carries the modification date, not the time).

Pure functions, no network.
"""

from __future__ import annotations

import datetime
import html
import re
import sqlite3
import unicodedata
from zoneinfo import ZoneInfo

KEY_PREFIX = "DBLP:"
WRAP_AT = 65  # a line is broken at the first space at content index >= 65
INDENT = " " * 18
BERLIN = ZoneInfo("Europe/Berlin")

ENTRY_TYPE = {
    "article": "article",
    "inproceedings": "inproceedings",
    "proceedings": "proceedings",
    "book": "book",
    "incollection": "incollection",
    "phdthesis": "phdthesis",
    "mastersthesis": "mastersthesis",
    "www": "misc",
    "data": "misc",
}

# ---------------------------------------------------------------------------
# Character conversion
# ---------------------------------------------------------------------------

# Accents dblp renders as LaTeX (combining mark -> macro), e.g. {\'{a}}, {\k{a}}.
ACCENT = {
    "\u0300": "`",  # grave
    "\u0301": "'",  # acute
    "\u0302": "^",  # circumflex
    "\u0303": "~",  # tilde
    "\u0304": "=",  # macron
    "\u0306": "u",  # breve
    "\u0307": ".",  # dot above
    "\u0308": '"',  # diaeresis
    "\u030a": "r",  # ring above
    "\u030b": "H",  # double acute
    "\u030c": "v",  # caron
    "\u0323": "d",  # dot below
    "\u0327": "c",  # cedilla
    "\u0328": "k",  # ogonek
}
# Letters without a decomposition.
SPECIAL = {
    "ß": "{\\ss}",
    "æ": "{\\ae}",
    "Æ": "{\\AE}",
    "ø": "{\\o}",
    "Ø": "{\\O}",
    "å": "{\\aa}",
    "Å": "{\\AA}",
    "œ": "{\\oe}",
    "Œ": "{\\OE}",
    "ł": "{\\l}",
    "Ł": "{\\L}",
    "ı": "{\\i}",
    "đ": "{\\dj}",
    "Đ": "{\\DJ}",
    # unverified against dblp output (no reference entry contains them):
    "ð": "{\\dh}",
    "Ð": "{\\DH}",
    "þ": "{\\th}",
    "Þ": "{\\TH}",
}
GREEK = {
    "α": "alpha",
    "β": "beta",
    "γ": "gamma",
    "δ": "delta",
    "ε": "epsilon",
    "ζ": "zeta",
    "η": "eta",
    "θ": "theta",
    "ι": "iota",
    "κ": "kappa",
    "λ": "lambda",
    "μ": "mu",
    "ν": "nu",
    "ξ": "xi",
    "π": "pi",
    "ρ": "rho",
    "σ": "sigma",
    "ς": "varsigma",
    "τ": "tau",
    "υ": "upsilon",
    "φ": "phi",
    "χ": "chi",
    "ψ": "psi",
    "ω": "omega",
    "ϵ": "epsilon",
    "ϑ": "vartheta",
    "ϕ": "phi",
    "ϱ": "varrho",
    "ϖ": "varpi",
    "Γ": "Gamma",
    "Δ": "Delta",
    "Θ": "Theta",
    "Λ": "Lambda",
    "Ξ": "Xi",
    "Π": "Pi",
    "Σ": "Sigma",
    "Υ": "Upsilon",
    "Φ": "Phi",
    "Ψ": "Psi",
    "Ω": "Omega",
}
ESCAPE = {"&": "{\\&}", "%": "{\\%}", "#": "{\\#}", "$": "{\\$}", "_": "{\\_}"}


def latex_char(ch: str) -> str:
    """LaTeX for one non-ASCII character (dblp conventions)."""
    if ch in SPECIAL:
        return SPECIAL[ch]
    if ch in GREEK:
        return "{\\(\\" + GREEK[ch] + "\\)}"
    decomp = unicodedata.normalize("NFD", ch)
    base, marks = decomp[0], decomp[1:]
    if base.isascii() and base.isalpha() and marks and all(m in ACCENT for m in marks):
        out = "\\i" if base == "i" else base
        for m in marks:
            out = "{\\" + ACCENT[m] + "{" + out + "}}"
        return out
    return ch


def to_latex(s: str) -> str:
    out = []
    for ch in s:
        if ch in ESCAPE:
            out.append(ESCAPE[ch])
        elif ord(ch) < 128:
            out.append(ch)
        else:
            out.append(latex_char(ch))
    return "".join(out)


# ---------------------------------------------------------------------------
# Capitalisation protection
# ---------------------------------------------------------------------------

PROTECT = re.compile(r"\(?[A-Z][A-Z0-9()\-./:]*|\([0-9][A-Z0-9()\-./:]*")


def protect(text: str) -> str:
    """Brace all-caps tokens (whitespace-delimited) such as ACM, {(IMLP)}, {K-12}.

    Tokens with a lowercase letter, a comma, or other punctuation stay as they
    are; a token ending in '.' (abbreviation) and a one-letter first word stay too.
    """
    toks = text.split(" ")
    out = []
    for i, tok in enumerate(toks):
        if PROTECT.fullmatch(tok) and not tok.endswith(".") and not (i == 0 and len(tok) == 1):
            tok = "{" + tok + "}"
        out.append(tok)
    return " ".join(out)


TAG = re.compile(r"</?(?:i|b|u|tt|em|sub|sup)>")
PH = {
    "<sub>": "\ue000",
    "</sub>": "\ue001",
    "<sup>": "\ue002",
    "</sup>": "\ue003",
    "<i>": "\ue004",
    "</i>": "\ue005",
}
PH_LATEX = re.compile("([\ue000\ue002\ue004])(.*?)[\ue001\ue003\ue005]", re.S)
PH_OPEN = {"\ue000": "\\({}_{\\mbox{", "\ue002": "\\({}^{\\mbox{", "\ue004": "\\emph{"}
PH_CLOSE = {"\ue000": "}}\\)", "\ue002": "}}\\)", "\ue004": "}"}


def text_field(s: str) -> str:
    """Title-like field: protect capitals, LaTeX-escape, convert markup.

    If the stored value kept dblp's inline XML markup (an XML fragment), then
    <sub>k</sub> becomes \\({}_{\\mbox{k}}\\), <sup> likewise with ^, <i>x</i>
    becomes \\emph{x}, and other tags (<tt>, <b>, ...) are dropped.  With
    flattened text the markup is lost and such titles differ from dblp.
    """
    marked = bool(TAG.search(s))
    if marked:
        s = html.unescape(TAG.sub(lambda m: PH.get(m.group(0), ""), s))
    out = to_latex(protect(s))
    if marked:
        out = PH_LATEX.sub(lambda m: PH_OPEN[m.group(1)] + m.group(2) + PH_CLOSE[m.group(1)], out)
    return out


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------

DISAMBIG = re.compile(r" \d{4}$")


def name_latex(name: str) -> str:
    name = DISAMBIG.sub("", name)
    return to_latex(name).replace("-", "{-}")


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------


def wrap(value: str) -> str:
    """dblp's line breaking: break at the first space at index >= WRAP_AT."""
    lines = []
    rest = value
    while len(rest) > WRAP_AT:
        k = rest.find(" ", WRAP_AT)
        if k < 0:
            break
        lines.append(rest[:k])
        rest = rest[k + 1 :]
    lines.append(rest)
    return ("\n" + INDENT).join(lines)


def field(name: str, value: str, wrapped: bool = True) -> str:
    # dblp quirk: "eprinttype" gets one extra space ("  eprinttype    = {", "=" in column
    # 17 instead of 16), as in raw dblp exports (evaluation/SUPP/results/*/mcp_u).
    pad = 13 if name == "eprinttype" else 12
    return f"  {name:<{pad}} = {{{wrap(value) if wrapped else value}}}"


def escape_url(s: str) -> str:
    """url and doi fields: _ -> \\_, % -> \\%, ~ -> \\%7E (dblp's escaping)."""
    return s.replace("_", "\\_").replace("%", "\\%").replace("~", "\\%7E")


def strip_period(s: str) -> str:
    return s[:-1] if s.endswith(".") else s


def timestamp(*mdates: str | None) -> str | None:
    """dblp's timestamp: the later of the record's and its crossref's mdate.

    The dump has no time of day, so 00:00:00 is used; the offset is that of
    Europe/Berlin on that date (dblp's server time zone).
    """
    dates = [m for m in mdates if m]
    if not dates:
        return None
    d = datetime.datetime.fromisoformat(max(dates)).replace(tzinfo=BERLIN)
    return d.strftime("%a, %d %b %Y %H:%M:%S %z")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

URN_EE = re.compile(r"nbn-resolving\.(?:org|de)/(urn:nbn:\S+)")
ECCC_EE = re.compile(r"eccc\.weizmann\.ac\.il/(?:eccc-)?reports?/(\d{4})/(\d+)")


def eccc_id(rec: dict) -> str | None:
    """ECCC report id TRyy-nnn from the ee (dblp uses it as volume and eprint)."""
    for ee in split_people(rec.get("ee")):
        m = ECCC_EE.search(ee)
        if m:
            return f"TR{m.group(1)[2:]}-{int(m.group(2)):03d}"
    return None


def split_people(s: str | None) -> list[str]:
    return [p for p in (s or "").split(" | ") if p]


def render(rec: dict, crossref_rec: dict | None = None, key_prefix: str = KEY_PREFIX) -> str:
    typ = rec["type"]
    etype = ENTRY_TYPE.get(typ, "misc")
    cr = crossref_rec or {}
    fields: list[tuple[str, str, bool]] = []

    authors = split_people(rec.get("authors"))
    editors = split_people(rec.get("editors"))
    if typ in ("inproceedings", "incollection") and not editors:
        editors = split_people(cr.get("editors"))
    if authors:
        fields.append(("author", (" and\n" + INDENT).join(name_latex(a) for a in authors), False))
    if editors:
        fields.append(("editor", (" and\n" + INDENT).join(name_latex(e) for e in editors), False))
    if rec.get("title"):
        fields.append(("title", text_field(strip_period(rec["title"])), True))

    if typ in ("inproceedings", "incollection"):
        bt = cr.get("title") or rec.get("booktitle")
        if bt:
            fields.append(("booktitle", text_field(strip_period(bt)), True))
        series = cr.get("series")
        volume = cr.get("volume")
        publisher = cr.get("publisher")
    else:
        series = rec.get("series")
        volume = rec.get("volume")
        publisher = rec.get("publisher")
        if rec["key"].startswith("journals/eccc/") and eccc_id(rec):
            volume = eccc_id(rec)
        if rec.get("journal"):
            fields.append(("journal", text_field(rec["journal"]), True))
    if series:
        fields.append(("series", text_field(series), True))
    if volume:
        fields.append(("volume", text_field(volume), True))
    if rec.get("number"):
        fields.append(("number", text_field(rec["number"]), True))
    if rec.get("pages") and typ != "book":
        fields.append(("pages", rec["pages"].replace("-", "--"), True))
    if publisher:
        fields.append(("publisher", text_field(publisher), True))
    if rec.get("school"):
        fields.append(("school", text_field(rec["school"]), True))
    year = rec.get("year") or cr.get("year")
    if year:
        fields.append(("year", str(year), True))
    ees = split_people(rec.get("ee"))
    if ees:
        fields.append(("url", escape_url(ees[0]), False))
    urn = next((m.group(1) for e in ees if (m := URN_EE.search(e))), None)
    if urn:
        fields.append(("urn", urn, False))
    if rec.get("doi"):
        fields.append(("doi", escape_url(rec["doi"].upper()), False))
    key = rec["key"]
    if key.startswith("journals/corr/") and (rec.get("volume") or "").startswith("abs/"):
        fields.append(("eprinttype", "arXiv", False))
        fields.append(("eprint", rec["volume"][4:], False))
    elif key.startswith("journals/eccc/") and eccc_id(rec):
        fields.append(("eprinttype", "ECCC", False))
        fields.append(("eprint", eccc_id(rec), False))
    if typ in ("book", "proceedings") and rec.get("isbn"):
        fields.append(("isbn", rec["isbn"], False))
    ts = timestamp(rec.get("mdate"), cr.get("mdate"))
    if ts:
        fields.append(("timestamp", ts, False))
    fields.append(("biburl", f"https://dblp.org/rec/{key}.bib", False))
    fields.append(("bibsource", "dblp computer science bibliography, https://dblp.org", False))

    body = ",\n".join(field(n, v, w) for n, v, w in fields)
    return f"@{etype}{{{key_prefix}{key},\n{body}\n}}\n"


# ---------------------------------------------------------------------------
# Database access
# ---------------------------------------------------------------------------


def load_record(conn: sqlite3.Connection, key: str) -> dict | None:
    # schema v2 (slim index): the view publ_v exposes the old column names/encodings
    has_view = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'publ_v'").fetchone()
    cur = conn.execute(f"SELECT * FROM {'publ_v' if has_view else 'publ'} WHERE key = ?", (key,))
    row = cur.fetchone()
    if row is None:
        return None
    return dict(zip([d[0] for d in cur.description], row, strict=True))


def bibtex_for_key(conn: sqlite3.Connection, key: str, key_prefix: str = KEY_PREFIX) -> str | None:
    rec = load_record(conn, key)
    if rec is None:
        return None
    cr = load_record(conn, rec["crossref"]) if rec.get("crossref") else None
    return render(rec, cr, key_prefix)
