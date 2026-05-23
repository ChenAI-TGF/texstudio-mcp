"""Optional BibTeX parsing via ``bibtexparser`` (extra dependency)."""

from __future__ import annotations

from collections import defaultdict
from typing import Any


def bibtexparser_available() -> bool:
    try:
        import bibtexparser  # noqa: F401

        return True
    except ImportError:
        return False


def validate_with_bibtexparser(text: str) -> dict[str, Any]:
    """Parse ``text`` with bibtexparser; return errors/warnings/duplicate_keys."""
    try:
        import bibtexparser
        from bibtexparser.bparser import BibTexParser
        from bibtexparser.customization import convert_to_unicode
    except ImportError as exc:
        return {
            "ok": False,
            "error": (
                "bibtexparser is not installed; pip install 'texstudio-mcp[bibtex]' "
                "or pip install bibtexparser"
            ),
            "import_error": str(exc),
        }

    parser = BibTexParser()
    parser.ignore_nonstandard_types = True
    parser.homogenise_fields = False
    parser.customization = convert_to_unicode

    try:
        library = bibtexparser.loads(text, parser=parser)
    except Exception as exc:  # bibtexparser may raise varied types
        return {
            "ok": False,
            "error": f"bibtexparser failed to parse: {exc}",
            "parse_exception_type": type(exc).__name__,
        }

    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for err in getattr(parser, "errors", []) or []:
        errors.append(
            {
                "code": "bibtexparser_parse_error",
                "message": str(err),
                "line": getattr(err, "lineno", None),
            }
        )

    key_lines: dict[str, list[int | None]] = defaultdict(list)
    for idx, entry in enumerate(library.entries):
        kid = entry.get("ID") or entry.get("id")
        if kid:
            key_lines[str(kid)].append(getattr(entry, "lineno", None))

    duplicate_keys: list[dict[str, Any]] = []
    for key, lines in sorted(key_lines.items()):
        if len(lines) > 1:
            duplicate_keys.append({"key": key, "lines": lines, "count": len(lines)})
            warnings.append(
                {
                    "code": "duplicate_citation_key",
                    "message": f"citation key {key!r} appears {len(lines)} times (bibtexparser)",
                    "key": key,
                    "lines": lines,
                }
            )

    return {
        "ok": len(errors) == 0,
        "errors": errors,
        "warnings": warnings,
        "duplicate_keys": duplicate_keys,
        "entry_count": len(library.entries),
        "validation_backend": "bibtexparser",
    }
