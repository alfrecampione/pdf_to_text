"""Utilities for extracting text from PDF files."""

from __future__ import annotations

import json
import pathlib
import re

import pdfplumber


def extract_pdf_to_text(
    pdf_path: str | pathlib.Path,
    output_path: str | pathlib.Path | None = None,
) -> str:

    pdf_path = pathlib.Path(pdf_path)
    if output_path is not None:
        output_path = pathlib.Path(output_path)
    parts: list[str] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines = text.splitlines()

            cleaned_lines = []
            for line in lines:
                if _should_drop_line(line):
                    continue
                cleaned_lines.append(line.strip())

            if cleaned_lines:
                parts.append("\n".join(cleaned_lines))
                parts.append("\n\n")

    content = "\n".join(part for part in parts if part)

    if output_path:
        output_path.write_text(content, encoding="utf-8")

    return content


_SEP_RE = re.compile(r"^[\.\-\u2014\u00b7\u2026]{6,}$")


def _is_separator(line: str) -> bool:
    """Detect separator rows made of repeated punctuation."""
    s = line.strip()
    return bool(s) and bool(_SEP_RE.match(s))


def _normalize_key(key: str) -> str:
    """Normalize keys to snake_case for JSON output."""
    key = key.lower()
    key = re.sub(r"[^a-z0-9]+", "_", key)
    return key.strip("_")


def _should_drop_line(line: str) -> bool:
    """Drop known header/footer noise (form codes, RPUID, Doc ID, page markers)."""
    s = line.strip()
    if not s:
        return False
    lower = s.lower()

    if lower.startswith("# page"):
        return True
    # Page header showing the policy number on every page is noise for parsers.
    if lower.startswith("policy number:"):
        return True
    if lower.startswith("form_"):
        return True
    if "rpuid" in lower:
        return True
    if lower.startswith("doc id:"):
        return True
    if lower.startswith("continued"):
        return True
    if re.match(r"^page \d+ of \d+", lower):
        return True
    if s.isdigit() and len(s) <= 3:
        return True
    return False


def _looks_like_person_name(text: str) -> bool:
    """Heuristic to detect human names (reject codes like '4' or headers)."""
    s = text.strip()
    if not s:
        return False
    if any(ch.isdigit() for ch in s):
        return False
    if any(ch in "$%#" for ch in s):
        return False
    tokens = s.split()
    if len(tokens) < 2:
        return False
    alpha_ratio = sum(ch.isalpha() for ch in s) / len(s)
    return alpha_ratio > 0.7


def _extract_key_values_line(line: str) -> list[tuple[str, str]]:
    """Extract multiple key:value pairs from a single line."""
    pairs: list[tuple[str, str]] = []
    # Allow letters, numbers, spaces, slashes, and hyphens in keys.
    pattern = re.compile(
        r"([A-Za-z0-9 /\-]+?):\s*([^:]+?)(?=(\s+[A-Za-z0-9/\-]+:\s)|$)"
    )
    for key, val, _ in pattern.findall(line):
        pairs.append((_normalize_key(key.strip()), val.strip()))
    return pairs


def _parse_coverage_row(line: str) -> dict[str, str] | None:
    """Parse a coverage row into coverage/limit/deductible/premium fields.

    The table is rendered as plain text, so we infer column boundaries using
    numeric tokens. We assume the last token is the premium and keep the entire
    middle chunk (limit/deductible text) verbatim without further parsing.
    """

    tokens = line.split()
    if len(tokens) < 2:
        return None

    premium = tokens[-1]
    body = tokens[:-1]

    def _has_digit(tok: str) -> bool:
        return any(ch.isdigit() for ch in tok)

    first_val_idx = None
    for i, tok in enumerate(body):
        if _has_digit(tok) or tok.lower() in {"rejected", "--"}:
            first_val_idx = i
            break

    if first_val_idx is None:
        return None

    coverage_tokens = body[:first_val_idx]
    middle_tokens = body[first_val_idx:]

    if not coverage_tokens:
        return None

    coverage = " ".join(coverage_tokens).strip()
    limit_text = " ".join(middle_tokens).strip()

    # Move trailing "Actual Cash Value" from coverage into limit text to avoid
    # embedding limit descriptors in the coverage name.
    if coverage.lower().endswith("actual cash value"):
        coverage = coverage[: -len("actual cash value")].strip()
        prefix = "Actual Cash Value"
        limit_text = f"{prefix} {limit_text}".strip()

    result: dict[str, str] = {"coverage": coverage, "premium": premium}
    if limit_text:
        result["limit"] = limit_text

    return result


def extract_policy_info_section(raw_text: str) -> dict[str, str]:
    """Extract the "Policy and premium information" block into a JSON-friendly dict.

    Returns a dict keyed by normalized labels (snake_case). Values preserve the
    raw text (joined with spaces) for each field.
    """

    lines = raw_text.splitlines()

    start_idx = None
    header_policy_number = None
    marker = "policy and premium information for policy number"
    for idx, line in enumerate(lines):
        lower = line.lower()
        if marker in lower:
            start_idx = idx
            m = re.search(r"policy number\s+([\w-]+)", lower)
            if m:
                header_policy_number = m.group(1)
            break

    if start_idx is None:
        return {}

    stop_markers = (
        "drivers and household residents",
        "# page",
    )

    result: dict[str, str] = {}
    if header_policy_number:
        result["policy_number"] = header_policy_number

    current_key: str | None = None
    buffer: list[str] = []

    for line in lines[start_idx + 1 :]:
        stripped = line.strip()
        lower = stripped.lower()

        if any(lower.startswith(sm) for sm in stop_markers):
            break

        if not stripped:
            continue

        if _is_separator(stripped):
            if current_key and buffer:
                result[current_key] = " ".join(buffer).strip()
                buffer = []
            continue

        if ":" in stripped:
            if current_key and buffer:
                result[current_key] = " ".join(buffer).strip()
                buffer = []

            key_part, value_part = stripped.split(":", 1)
            current_key = _normalize_key(key_part)
            if value_part.strip():
                buffer.append(value_part.strip())
        else:
            if current_key:
                buffer.append(stripped)

    if current_key and buffer:
        result[current_key] = " ".join(buffer).strip()

    return result


def extract_drivers_section(raw_text: str) -> list[dict[str, str]]:
    lines = raw_text.splitlines()
    start_idx = None

    for idx, line in enumerate(lines):
        if "drivers and household residents" in line.lower():
            start_idx = idx
            break

    if start_idx is None:
        return []

    drivers: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    has_fields = False

    for line in lines[start_idx + 1 :]:
        stripped = line.strip()
        lower = stripped.lower()

        if not stripped:
            continue

        if lower.startswith("# page"):
            continue

        if "outline of coverage" in lower:
            break

        if _is_separator(stripped):
            continue

        # Nombre detectado (solo si no hay "key:")
        if ":" not in stripped and _looks_like_person_name(stripped):
            if current and has_fields:
                drivers.append(current)

            current = {"name": stripped}
            has_fields = False
            continue

        # Campos key:value
        kv_pairs = _extract_key_values_line(stripped)
        if kv_pairs:
            if current is None:
                continue  # no aceptamos campos sin nombre previo

            for k, v in kv_pairs:
                current[k] = v
            has_fields = True

    if current and has_fields:
        drivers.append(current)

    return drivers


def extract_outline_of_coverage(raw_text: str) -> list[dict[str, object]]:
    """Extract vehicles and their coverages from the "Outline of coverage" section."""

    lines = raw_text.splitlines()
    start_idx = None

    for idx, line in enumerate(lines):
        if "outline of coverage" in line.lower():
            start_idx = idx
            break

    if start_idx is None:
        return []

    vehicles: list[dict[str, object]] = []
    current: dict[str, object] | None = None
    coverages: list[dict[str, str]] = []
    mode: str = "details"  # details | coverages | ignore_discounts

    def _flush_current() -> None:
        nonlocal current, coverages
        if current is None:
            return
        if coverages:
            current["coverages"] = coverages
        vehicles.append(current)
        current = None
        coverages = []

    idx = start_idx + 1
    while idx < len(lines):
        line = lines[idx].strip()
        lower = line.lower()

        if not line:
            idx += 1
            continue

        if "driving history" in lower:
            _flush_current()
            break

        # Start of a new vehicle.
        if re.match(r"^\d{4}\s", line):
            _flush_current()
            current = {"vehicle": line}
            mode = "details"
            idx += 1
            continue

        if current is None:
            idx += 1
            continue

        if _is_separator(line):
            idx += 1
            continue

        if lower.startswith("vin:"):
            current["vin"] = line.split(":", 1)[1].strip()
        elif lower.startswith("garaging zip code:"):
            current["garaging_zip_code"] = line.split(":", 1)[1].strip()
        elif lower.startswith("primary use of the vehicle:"):
            current["primary_use"] = line.split(":", 1)[1].strip()
        elif lower.startswith("annual miles:"):
            current["annual_miles"] = line.split(":", 1)[1].strip()
        elif lower.startswith("length of vehicle ownership"):
            current["length_of_vehicle_ownership"] = line.split(":", 1)[1].strip()
        elif lower.startswith("limits deductible premium"):
            mode = "coverages"
        elif lower.startswith("premium discounts"):
            _flush_current()
            break
        elif lower.startswith("total") and "premium" in lower:
            # Example: Total 6 month policy premium $1,436.00
            current["total_premium"] = line.split()[-1]
        elif mode == "coverages":
            parsed = _parse_coverage_row(line)
            if parsed:
                coverages.append(parsed)
        elif mode == "ignore_discounts":
            idx += 1
            continue

        idx += 1

    _flush_current()
    return vehicles


if __name__ == "__main__":

    result = extract_pdf_to_text(
        "pdf1.pdf",
        "test.txt",
    )
