from __future__ import annotations
import pathlib
import re
from typing import Any
import pdfplumber

_SEP_RE = re.compile(r"^[\.\-\u2014\u00b7\u2026]{6,}$")


def _is_separator(line: str) -> bool:
    """Detect separator rows made of repeated punctuation."""
    s = line.strip()
    return bool(s) and bool(_SEP_RE.match(s))


def _looks_like_heading(line: str) -> bool:
    """Heuristic to detect section headings (e.g., Policy, Vehicle, Other)."""

    if not line:
        return False

    if any(ch.isdigit() for ch in line):
        return False

    if ":" in line:
        return False

    tokens = line.split()
    if len(tokens) <= 4 and all(tok.isalpha() for tok in tokens):
        return True

    return False


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


def extract_pdf_to_text(pdf_path: str | pathlib.Path) -> str:
    pdf_path = pathlib.Path(pdf_path)
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

    return content


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


def _extract_discount_value(line: str) -> str | None:
    """Get the right-column discount text by dropping leading identifiers."""

    tokens = line.split()
    if not tokens:
        return None

    start_idx = None
    for idx, tok in enumerate(tokens):
        if any(ch.islower() for ch in tok):
            start_idx = idx
            break

    if start_idx is None:
        return None

    return " ".join(tokens[start_idx:]).strip()


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

        if lower.startswith("total residents"):
            idx += 1
            continue

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


def extract_premium_discounts(raw_text: str) -> list[dict[str, str]]:
    """Extract discounts grouped by any heading into structured rows."""

    lines = raw_text.splitlines()
    start_idx = None
    for idx, line in enumerate(lines):
        if "premium discounts" in line.lower():
            start_idx = idx
            break

    if start_idx is None:
        return []

    stop_markers = (
        "driving history",
        "underwriting information",
        "application agreement",
        "verification of content",
        "notice of information",
    )

    results: list[dict[str, str]] = []
    current_heading: str | None = None

    def _key_field(row: dict[str, str]) -> str | None:
        for k in row:
            if k != "discount":
                return k
        return None

    idx = start_idx + 1
    while idx < len(lines):
        line = lines[idx]
        stripped = line.strip()
        lower = stripped.lower()

        if not stripped:
            idx += 1
            continue

        if any(lower.startswith(sm) for sm in stop_markers):
            break

        if _is_separator(stripped):
            idx += 1
            continue

        if _looks_like_heading(stripped):
            current_heading = stripped.strip()
            idx += 1
            continue

        if current_heading is None:
            idx += 1
            continue

        # Collect a logical row: consecutive non-blank, non-heading, non-separator lines.
        row_lines = [stripped]
        look_ahead = idx + 1
        while look_ahead < len(lines):
            nxt = lines[look_ahead]
            nxt_strip = nxt.strip()
            nxt_lower = nxt_strip.lower()

            if not nxt_strip:
                break
            if _is_separator(nxt_strip):
                break
            if _looks_like_heading(nxt_strip):
                break
            if any(nxt_lower.startswith(sm) for sm in stop_markers):
                break

            row_lines.append(nxt_strip)
            look_ahead += 1

        idx = look_ahead

        row_text = " ".join(row_lines)
        tokens = row_text.split()
        if not tokens:
            continue

        # Split key/value by finding the token boundary that best balances left (key) vs right (value).
        # Aim for right side to be longer; use a simple scoring heuristic.
        def _segment_score(left: str, right: str) -> int:
            # Prefer right ~ 1.5x left; penalize empty sides.
            if not left or not right:
                return 10_000
            return abs(len(right) - int(1.5 * len(left)))

        best_idx = None
        best_score = 10_000
        for i in range(1, len(tokens)):
            left = " ".join(tokens[:i])
            right = " ".join(tokens[i:])
            score = _segment_score(left, right)
            if score < best_score:
                best_score = score
                best_idx = i

        split_idx = best_idx if best_idx is not None else max(1, len(tokens) // 2)

        key_text = " ".join(tokens[:split_idx]).strip()
        discount_text = " ".join(tokens[split_idx:]).strip()

        heading_key = current_heading.strip() if current_heading else "Item"

        if results and key_text and key_text == results[-1].get(heading_key):
            # Same key as previous row: treat as continuation of value.
            results[-1]["discount"] = (
                f"{results[-1]['discount']} {discount_text}"
            ).strip()
        else:
            results.append({heading_key: key_text, "discount": discount_text})

    return results


def extract_underwriting_information(raw_text: str) -> dict[str, str]:
    """Extract key/value pairs from the "Underwriting information" section."""

    lines = raw_text.splitlines()
    start_idx = None

    for idx, line in enumerate(lines):
        if "underwriting information" in line.lower():
            start_idx = idx
            break

    if start_idx is None:
        return {}

    stop_markers = (
        "application agreement",
        "verification of content",
        "notice of information",
        "personal injury protection",
        "# page",
    )

    result: dict[str, str] = {}
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


def build_policy_data(pdf_path: str | pathlib.Path) -> dict[str, Any]:
    """Extract all structured sections from a Progressive policy PDF."""

    raw_text = extract_pdf_to_text(pdf_path)

    with open("debug_extracted_text.txt", "w", encoding="utf-8") as f:
        f.write(raw_text)

    policy = extract_policy_info_section(raw_text)
    drivers = extract_drivers_section(raw_text)
    outline = extract_outline_of_coverage(raw_text)
    discounts = extract_premium_discounts_from_pdf(str(pdf_path))
    underwriting = extract_underwriting_information(raw_text)

    return {
        "policy": policy,
        "drivers": drivers,
        "outline": outline,
        "discounts": discounts,
        "underwriting": underwriting,
    }


def extract_premium_discounts_from_pdf(pdf_path: str) -> list[dict[str, str]]:
    """Extract premium discounts by grouping words into rows using coordinates."""

    def _is_stop(text: str) -> bool:
        lower = text.lower()
        return lower.startswith("driving history") or lower.startswith(
            "underwriting information"
        )

    def _is_heading_text(text: str) -> bool:
        return _looks_like_heading(text.strip())

    results: list[dict[str, str]] = []

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words() or []
            if not words:
                continue

            # Locate vertical bounds of the discounts section.
            lines_meta: list[tuple[float, float, str]] = []  # (top, bottom, text)
            by_top: dict[float, list[dict[str, object]]] = {}
            for w in words:
                top = float(w.get("top", 0.0))
                by_top.setdefault(round(top, 1), []).append(w)

            for top_key, items in by_top.items():
                items_sorted = sorted(items, key=lambda w: float(w.get("x0", 0.0)))
                text = " ".join(w.get("text", "") for w in items_sorted)
                bottom = max(float(w.get("bottom", top_key)) for w in items_sorted)
                lines_meta.append((top_key, bottom, text))

            lines_meta.sort(key=lambda t: t[0])
            start_line = next(
                (ln for ln in lines_meta if "premium discounts" in ln[2].lower()), None
            )
            stop_line = next((ln for ln in lines_meta if _is_stop(ln[2])), None)
            if start_line is None:
                continue

            start_y = start_line[0]
            stop_y = stop_line[0] if stop_line else page.height

            section_words = [
                w
                for w in words
                if float(w.get("top", 0.0)) >= start_y
                and float(w.get("bottom", 0.0)) <= stop_y
            ]
            if not section_words:
                continue

            # Group words into text lines by vertical proximity.
            by_line: dict[float, list[dict[str, object]]] = {}
            for w in section_words:
                top = float(w.get("top", 0.0))
                # tolerance of 1.0 pt to keep same line
                key = round(top, 1)
                by_line.setdefault(key, []).append(w)

            text_lines: list[dict[str, object]] = []
            for key, items in by_line.items():
                sorted_items = sorted(items, key=lambda w: float(w.get("x0", 0.0)))
                text = " ".join(w.get("text", "") for w in sorted_items)
                bottom = max(float(w.get("bottom", key)) for w in sorted_items)
                text_lines.append(
                    {"top": key, "bottom": bottom, "words": sorted_items, "text": text}
                )

            text_lines.sort(key=lambda t: t["top"])

            # Group lines into rows (rows can span multiple lines) using vertical spacing.
            gaps = [
                text_lines[i + 1]["top"] - text_lines[i]["bottom"]
                for i in range(len(text_lines) - 1)
            ]
            median_gap = sorted(gaps)[len(gaps) // 2] if gaps else 0.0
            row_gap_tol = max(4.0, median_gap * 2 or 4.0)

            rows: list[list[dict[str, object]]] = []
            current_row: list[dict[str, object]] = []
            last_bottom: float | None = None

            for ln in text_lines:
                if last_bottom is None or ln["top"] - last_bottom <= row_gap_tol:
                    current_row.append(ln)
                    last_bottom = ln["bottom"]
                else:
                    if current_row:
                        rows.append(current_row)
                    current_row = [ln]
                    last_bottom = ln["bottom"]
            if current_row:
                rows.append(current_row)

            # Estimate gutter (column split) as mid-point of the text block.
            x0_min = min(float(w.get("x0", 0.0)) for w in section_words)
            x1_max = max(
                float(w.get("x1", float(w.get("x0", 0.0)))) for w in section_words
            )
            # Shift gutter further left to keep value tokens out of the key column
            gutter_x = x0_min + 0.42 * (x1_max - x0_min)

            current_heading: str | None = None
            heading_prefixes = ("policy", "vehicle")
            sep_re = re.compile(r"^[\.\u00b7\u2026\u2014·]{3,}$")

            for row in rows:
                # Flatten row words and text.
                row_words = []
                for ln in row:
                    row_words.extend(ln["words"])

                if not row_words:
                    continue

                row_words_sorted = sorted(
                    row_words, key=lambda w: float(w.get("x0", 0.0))
                )
                row_text = " ".join(w.get("text", "") for w in row_words_sorted).strip()
                row_text_clean = re.sub(
                    r"[\.\u00b7\u2026\u2014·]{3,}", " ", row_text
                ).strip()

                if not row_text_clean:
                    continue

                if _is_stop(row_text_clean):
                    break

                lower_clean = row_text_clean.lower()

                # Skip metadata/noise rows (page markers, doc IDs, continued markers).
                if (
                    "doc id" in lower_clean
                    or "continued" in lower_clean
                    or lower_clean.startswith("# page")
                ):
                    continue

                # Detect headings possibly followed by data on the same row (Policy ..., Vehicle ...)
                heading_detected: str | None = None
                for pref in heading_prefixes:
                    if lower_clean.startswith(pref):
                        heading_detected = pref.capitalize()
                        break

                if heading_detected:
                    current_heading = heading_detected
                    # Remove heading token from words for downstream split
                    row_words_sorted = [
                        w
                        for w in row_words_sorted
                        if w.get("text", "").lower() != heading_detected.lower()
                        and not sep_re.match(w.get("text", ""))
                    ]
                    # If after removing heading there is no data, move to next row
                    if not row_words_sorted:
                        continue

                elif _is_heading_text(row_text_clean):
                    current_heading = row_text_clean
                    continue

                if current_heading is None:
                    continue

                filtered_words = [
                    w for w in row_words_sorted if not sep_re.match(w.get("text", ""))
                ]

                left_words = [
                    w for w in filtered_words if float(w.get("x0", 0.0)) < gutter_x
                ]
                right_words = [
                    w for w in filtered_words if float(w.get("x0", 0.0)) >= gutter_x
                ]

                left_text = " ".join(w.get("text", "") for w in left_words).strip()
                right_text = " ".join(w.get("text", "") for w in right_words).strip()

                heading_key = current_heading.strip() if current_heading else "Item"

                if not right_text and results:
                    # Continuation row: append text to previous discount.
                    results[-1][
                        "discount"
                    ] = f"{results[-1]['discount']} {left_text}".strip()
                    continue

                if right_text and not left_text and results:
                    # No key, only value -> continuation of previous value.
                    results[-1][
                        "discount"
                    ] = f"{results[-1]['discount']} {right_text}".strip()
                    continue

                if not left_text or not right_text:
                    continue

                results.append({heading_key: left_text, "discount": right_text})

    return results
