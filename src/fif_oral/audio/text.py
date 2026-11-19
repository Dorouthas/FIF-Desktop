import re


SINGLE_WORD = re.compile(r"[A-Za-z]+(?:['’-][A-Za-z]+)*[.!?]?")
VERSION = "legacy-text-v1"


def normalize(text):
    """Match the old client's currency, ordinal and decimal reading rules."""
    from num2words import num2words

    def currency(match):
        unit = {"$": "dollar", "€": "euro", "£": "pound", "￡": "pound", "¥": "yen", "￥": "yuan"}[match[1]]
        parts = match[2].replace(",", "").split(".")
        integer = int(parts[0])
        suffix = unit if unit in {"yen", "yuan"} or integer == 1 else unit + "s"
        result = f"{num2words(integer, lang='en')} {suffix}"
        if len(parts) == 2 and unit not in {"yen", "yuan"}:
            cents = int((parts[1] + "00")[:2])
            result += f" and {num2words(cents, lang='en')} cents"
        return result

    text = re.sub(r"([$€£￡¥￥])\s*(\d[\d,]*(?:\.\d+)?)", currency, text)
    text = re.sub(r"(?<!\w)(\d+(?:,\d{3})*)(?:st|nd|rd|th)(?!\w)",
                  lambda m: num2words(int(m[1].replace(",", "")), to="ordinal", lang="en"), text, flags=re.I)

    def number(match):
        parts = match[0].replace(",", "").split(".")
        result = num2words(int(parts[0]), lang="en")
        if len(parts) == 2:
            result += " point " + " ".join(num2words(int(digit), lang="en") for digit in parts[1])
        return result

    return re.sub(r"(?<!\w)(?:\d{1,3}(?:,\d{3})*(?:\.\d+)?|\d+(?:\.\d+)?)(?!\w)", number, text)
