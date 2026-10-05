"""Shared "--flag [value]" grammar for our tools (gcb-style, see
Vision03__get_codeblock.md "Грамматика вызова" and
BRAINSTORM__read_patch.md §8) - a single free-text `flags` schema field
instead of a growing pile of individual JSON booleans.
"""

from __future__ import annotations

try:  # plugin mode
    from . import _vocab
except ImportError:  # console mode
    import _vocab  # type: ignore


# Every pair a model has been seen to reach for, plus the ones it plausibly
# would. Backticks because markdown is its native habitat; the typographic and
# guillemet pairs because a Russian-language turn produces them without the
# model noticing. Same-character pairs (") and distinct pairs («») are both
# here, which is why this is a MAP and not a set.
QUOTE_PAIRS = {
    '"': '"', "'": "'", "`": "`",
    "“": "”",   # “ ”
    "‘": "’",   # ‘ ’
    "«": "»",   # « »
    "‹": "›",   # ‹ ›
}


def is_quotes_only(value: str) -> bool:
    """True for a value that is NOTHING BUT an empty pair of quotes.

    Its own function because two callers need it and the intent is easy to
    misread as "starts with a quote": `""`, ``, '', «» all mean the model was
    trying to say EMPTY - and it means that because our own schema used to
    print `""` as the way to say it.
    """
    value = (value or "").strip()
    return len(value) == 2 and QUOTE_PAIRS.get(value[0]) == value[1]


def _unquote(value: str) -> str:
    """Drop ONE layer of matching surrounding quotes from a flag's value.

    The string is written like a shell command, so a model quotes values the
    way it would in a shell - and here the quotes are literal characters, not
    syntax. Live evidence (agent run 2026-09-03): `--count "3"` and
    `--glob "*.txt"`. The first failed LOUDLY (`expects a number, got "3"`) and
    the agent corrected itself; the second failed SILENTLY - the glob became
    `"*.txt"`, fnmatch matched nothing, and `Found: 0` reads as a legitimate
    answer. The agent then wrote down a false rule about our tool, which is the
    real cost: a wrong belief outlives the call.

    One layer only, and only when both ends match: a value that is genuinely
    supposed to contain a quote (a pattern for a quoted string, say) keeps it.
    """
    if len(value) >= 2 and QUOTE_PAIRS.get(value[0]) == value[-1]:
        return value[1:-1]
    return value


def parse_flags(flags: str) -> dict:
    """Same as `parse_flags_report`, dropping the count. Most callers only want
    the flags; the ones that TELL the model about its quoting use the report."""
    return parse_flags_report(flags)[0]


def parse_flags_report(flags: str):
    """'--force --some-other o1,o2,o3' -> {'force': True, 'some-other': 'o1,o2,o3'}

    A bare flag (followed by another '--...' token, or the end of the
    string) becomes True. A flag followed by a non-'--' token gets that
    token as its raw string value - callers split it further (e.g. on ',')
    however makes sense for that specific flag; this parser doesn't guess.
    """
    if not flags:
        return {}, 0
    # The WHOLE string first, then each value. In a tool-calling template with
    # no quoting layer of its own (the `<parameter=flags>` XML form these local
    # models use), everything between the tags is literal - so a model quoting
    # the way it would in JSON sends `"--numbered"`, and without this every
    # flag in the string is lost SILENTLY: no token starts with `--`, so the
    # parser returns {}. Seen live 2026-09-03 in three calls, and it was worse
    # than a lost flag - the agent's own report claimed `--numbered` and
    # `--context 2` had worked.
    stripped = 0
    whole = flags.strip()
    unwrapped = _unquote(whole)
    if unwrapped != whole:
        stripped += 1
    tokens = unwrapped.split()
    result = {}
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if not tok.startswith("--"):
            i += 1
            continue
        name = tok[2:]
        if i + 1 < len(tokens) and not tokens[i + 1].startswith("--"):
            value = _unquote(tokens[i + 1])
            if value != tokens[i + 1]:
                stripped += 1
            result[name] = value
            i += 2
        else:
            result[name] = True
            i += 1
    return result, stripped


QUOTE_HINT = (f'{_vocab.COMMON["hint"]}: quotes around the flag string/values were removed - write '
              'them bare (--count 20, not --count "20"), the value is taken '
              'literally including any quote characters.')


def has_flag(flags: str, name: str) -> bool:
    return bool(parse_flags(flags).get(name))


class FlagError(ValueError):
    """A flag was written in a way we cannot act on - message is for the MODEL.

    Raised, not returned, so a core can convert several flag reads into one
    "ERROR: <tool>: ..." answer at a single place instead of threading an
    error value through every extraction.
    """


def flag_int(parsed: dict, name: str, default: int) -> int:
    """`{'count': '134'}` -> 134. Junk raises FlagError instead of quietly
    falling back to the default: a number that silently became 2000 is exactly
    the "parameter was ignored" confusion Plan08 exists to remove.
    """
    raw = parsed.get(name)
    if raw is None:
        return default
    if raw is True:  # bare "--count" with nothing after it
        return _raise_needs_number(name)
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return _raise_needs_number(name, raw)


def _raise_needs_number(name: str, raw=None) -> int:
    got = "" if raw is None else f' got "{raw}".'
    raise FlagError(f'--{name} expects a number, e.g. "--{name} 20".{got}')


_TRUTHY = {"true", "1", "yes", "on"}
_FALSY = {"false", "0", "no", "off"}


def flag_bool(parsed: dict, name: str, default: bool = False) -> bool:
    """A switch: `--numbered` alone is True. `--numbered true/false` is also
    accepted, because a model that knows the parameter as a JSON boolean writes
    it that way; anything else is an error naming the flag rather than a value
    read as some unrelated flag's argument.
    """
    raw = parsed.get(name)
    if raw is None:
        return default
    if raw is True:
        return True
    word = str(raw).strip().lower()
    if word in _TRUTHY:
        return True
    if word in _FALSY:
        return False
    raise FlagError(f'--{name} is a switch: write "--{name}" on its own, '
                    f'not "--{name} {raw}".')


def unknown_flags(parsed: dict, known) -> list:
    """Flag names we do not recognise, in the order given.

    Callers REPORT these; they must not ignore them. A typo (including
    "--count=134", which parses as a flag literally named "count=134") would
    otherwise look like a parameter that was accepted and had no effect.
    """
    return [name for name in parsed if name not in known]
