from progressive import (
    extract_policy_info_section as progressive_extract_policy_info_section,
    extract_drivers_section as progressive_extract_drivers_section,
    extract_outline_of_coverage as progressive_extract_outline_of_coverage,
    extract_premium_discounts as progressive_extract_premium_discounts,
    extract_premium_discounts_from_pdf,
)

import json
import pathlib
import pdfplumber
import re


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


import os

if __name__ == "__main__":

    pdf_folder = os.listdir("./pdfs")
    pdf_file_array: list[str] = []

    for pdf_file in pdf_folder:
        if not pdf_file.endswith(".pdf"):
            continue
        pdf_file_array.append(pdf_file)

    for pdf_file in pdf_file_array:
        print(f"Processing file: {pdf_file}")

        result = extract_pdf_to_text(f"./pdfs/{pdf_file}")

        policy = progressive_extract_policy_info_section(result)
        drivers = progressive_extract_drivers_section(result)
        outline = progressive_extract_outline_of_coverage(result)
        discounts = extract_premium_discounts_from_pdf(
            f"./pdfs/{pdf_file}"
        ) or progressive_extract_premium_discounts(result)

        output = {
            "policy": policy,
            "drivers": drivers,
            "outline": outline,
            "discounts": discounts,
        }

        with open(f"./outputs/{pdf_file[: -4]}.json", "x") as output_file:
            json.dump(output, output_file, indent=4)
