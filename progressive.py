from __future__ import annotations
import re
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

        tokens = stripped.split()
        if not tokens:
            idx += 1
            continue

        # Stop if we reach another section once we've collected rows.
        if (
            results
            and not line[:1].isspace()
            and current_heading
            and not tokens[0].isdigit()
        ):
            break

        # If this is a continuation line, append to last discount text.
        if results and line[:1].isspace():
            results[-1]["discount"] = f"{results[-1]['discount']} {stripped}".strip()
            idx += 1
            continue

        # Generic row: split tokens into key (left column) and discount (right column) at first lowercase token.
        split_idx = None
        for i, tok in enumerate(tokens):
            if any(ch.islower() for ch in tok):
                split_idx = i
                break

        if split_idx is None or split_idx == 0:
            # Continuation: append to the last parsed row.
            if results:
                key_field = _key_field(results[-1])
                if key_field:
                    first_tok = tokens[0]
                    rest = tokens[1:]

                    if not any(ch.islower() for ch in first_tok):
                        results[-1][
                            key_field
                        ] = f"{results[-1][key_field]} {first_tok}".strip()
                    else:
                        rest.insert(0, first_tok)

                    if rest:
                        results[-1][
                            "discount"
                        ] = f"{results[-1]['discount']} {' '.join(rest)}".strip()
            idx += 1
            continue

        key_text = " ".join(tokens[:split_idx]).strip()
        discount_text = " ".join(tokens[split_idx:]).strip()

        heading_key = current_heading.strip() if current_heading else "Item"
        results.append({heading_key: key_text, "discount": discount_text})
        idx += 1
        continue

        idx += 1

    return results


def extract_premium_discounts_from_pdf(pdf_path: str) -> list[dict[str, str]]:
    """Extract premium discounts directly from a PDF using page bounding boxes."""
    results: list[dict[str, str]] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            words = page.extract_words() or []
            if not words:
                continue
            lines: list[tuple[float, float, float, str]] = []  # (top, bottom, x0, text)
            by_top: dict[float, list[dict[str, object]]] = {}
            for w in words:
                top = float(w.get("top", 0.0))
                by_top.setdefault(round(top, 1), []).append(w)
            for top_key, items in by_top.items():
                items_sorted = sorted(items, key=lambda w: float(w.get("x0", 0.0)))
                text = " ".join(w.get("text", "") for w in items_sorted)
                bottom = max(float(w.get("bottom", top_key)) for w in items_sorted)
                x0 = float(items_sorted[0].get("x0", 0.0)) if items_sorted else 0.0
                lines.append((top_key, bottom, x0, text))
            lines_sorted = sorted(lines, key=lambda t: (t[0], t[2]))
            start_line = next(
                (ln for ln in lines_sorted if "premium discounts" in ln[3].lower()),
                None,
            )
            stop_line = next(
                (ln for ln in lines_sorted if "driving history" in ln[3].lower()), None
            )
            if start_line is None:
                continue
            start_y = start_line[1]
            stop_y = stop_line[0] if stop_line else page.height
            crop = page.crop((0, start_y, page.width, stop_y))
            crop_text = crop.extract_text() or ""
            part = extract_premium_discounts(crop_text)
            if part:
                results.extend(part)
    return results
