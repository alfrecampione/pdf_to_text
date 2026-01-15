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


if __name__ == "__main__":

    doc = "pdf3"

    result = extract_pdf_to_text(
        f"{doc}.pdf",
        f"{doc}.txt",
    )

    result1 = progressive_extract_policy_info_section(result)
    result2 = progressive_extract_drivers_section(result)
    result3 = progressive_extract_outline_of_coverage(result)
    # Prefer PDF-based extraction for premium discounts to preserve table structure.
    result4 = extract_premium_discounts_from_pdf(
        f"{doc}.pdf"
    ) or progressive_extract_premium_discounts(result)
    print("-------- POLICY INFO --------")
    print(json.dumps(result1, indent=2))
    print("-------- DRIVERS --------")
    print(json.dumps(result2, indent=2))
    print("-------- OUTLINE OF COVERAGE --------")
    print(json.dumps(result3, indent=2))
    print("-------- PREMIUM DISCOUNTS --------")
    print(json.dumps(result4, indent=2))
