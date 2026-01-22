from progressive import (
    extract_policy_info_section as progressive_extract_policy_info_section,
    extract_drivers_section as progressive_extract_drivers_section,
    extract_outline_of_coverage as progressive_extract_outline_of_coverage,
    extract_premium_discounts as progressive_extract_premium_discounts,
    extract_premium_discounts_from_pdf as progressive_extract_premium_discounts_from_pdf,
)

import json
import os
import pathlib
import re
import tempfile
from typing import Any

import pdfplumber
import requests
import sys


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


def _download_pdf_from_s3(s3_url: str) -> pathlib.Path:
    """Download the PDF from an S3 URL to a temporary file and return the path."""

    response = requests.get(s3_url, stream=True, timeout=60)
    response.raise_for_status()

    with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                tmp.write(chunk)
        return pathlib.Path(tmp.name)


def _build_output(pdf_path: str | pathlib.Path) -> dict[str, Any]:
    """Run all parsers and return the consolidated JSON-friendly object."""

    raw_text = extract_pdf_to_text(pdf_path)
    policy = progressive_extract_policy_info_section(raw_text)
    drivers = progressive_extract_drivers_section(raw_text)
    outline = progressive_extract_outline_of_coverage(raw_text)
    discounts = progressive_extract_premium_discounts_from_pdf(str(pdf_path))

    return {
        "policy": policy,
        "drivers": drivers,
        "outline": outline,
        "discounts": discounts,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python pdf_converter.py <s3_pdf_url>")
        raise SystemExit(1)

    s3_url = sys.argv[1]

    pdf_tmp_path = _download_pdf_from_s3(s3_url)

    try:
        output = _build_output(pdf_tmp_path)
    finally:
        try:
            os.remove(pdf_tmp_path)
        except OSError:
            pass

    print(json.dumps(output, indent=4))
