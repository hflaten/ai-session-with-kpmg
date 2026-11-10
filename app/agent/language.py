"""Which language to answer in, decided in code from the user's latest message.

Norwegian if the text has æ, ø or å, or at least two common Norwegian words; otherwise English.
(The case background is in Norwegian, so leaving it to the model made English questions
sometimes get Norwegian answers.)
"""
import re

NORWEGIAN_WORDS = {
    "og", "ikke", "hva", "hvor", "hvilke", "hvilken", "hvordan", "hvorfor", "hvem", "når", "vi", "jeg", "dere",
    "skal", "bør", "må", "kan", "har", "blir", "var", "er", "det", "den", "som", "med", "til", "av", "på", "fra",
    "mye", "mange", "noen", "eller", "hvis", "om", "neste", "uke", "uker", "dag", "dager", "jul", "julen",
    "bestille", "bestilling", "bestillinger", "lager", "svinn", "utsolgt", "butikk", "butikkene", "varer", "vare",
}


def detect_language(text: str) -> str:
    """'Norwegian' or 'English'."""
    if re.search(r"[æøåÆØÅ]", text or ""):
        return "Norwegian"
    words = re.findall(r"[a-zA-Z]+", (text or "").lower())
    return "Norwegian" if sum(w in NORWEGIAN_WORDS for w in words) >= 2 else "English"


def language_instruction(text: str) -> str:
    return f"Answer in {detect_language(text)}."
