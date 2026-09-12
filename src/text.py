def plural(count, word):
    return "%d %s%s" % (count, word, "" if count == 1 else "s")
