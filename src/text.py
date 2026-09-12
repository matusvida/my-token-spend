def plural(count, word):
    return "%d %s%s" % (count, word, "" if count == 1 else "s")


def bounded(low, high, word):
    if low == high:
        return "exactly %s" % plural(low, word)
    return "between %d and %s" % (low, plural(high, word))
