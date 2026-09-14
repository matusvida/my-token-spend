def plural(count, word):
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def bounded(low, high, word):
    if low == high:
        return "exactly %s" % plural(low, word)
    return "between %d and %s" % (low, plural(high, word))


_MOJIBAKE_LEAD = frozenset(chr(code) for code in range(0xC2, 0xF5))


def repair_mojibake(value):
    if not value or not any(char in _MOJIBAKE_LEAD for char in value):
        return value
    try:
        return value.encode("cp1252", errors="strict").decode("utf-8", errors="strict")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value
