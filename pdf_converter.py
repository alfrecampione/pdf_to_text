from progressive import build_policy_data as progressive_build_policy_data

import json
import os
import pathlib
import tempfile
import sys

from dotenv import load_dotenv
import boto3
from urllib.parse import urlparse


load_dotenv()


def _download_pdf_from_s3(s3_url: str) -> pathlib.Path:
    """
    Download a private S3 PDF using AWS credentials from .env
    Windows-safe: avoid NamedTemporaryFile locking issues.
    """
    from urllib.parse import urlparse
    import boto3
    import os
    import tempfile
    import pathlib

    parsed = urlparse(s3_url)

    # qqcatalyst-files.s3.us-east-1.amazonaws.com -> qqcatalyst-files
    bucket = parsed.netloc.split(".")[0]
    key = parsed.path.lstrip("/")

    s3 = boto3.client(
        "s3",
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=os.getenv("AWS_REGION", "us-east-1"),
    )

    # Create a temp filename without keeping it open (prevents WinError 32)
    fd, tmp_path = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)  # critical on Windows

    tmp_path = pathlib.Path(tmp_path)
    s3.download_file(bucket, key, str(tmp_path))
    return tmp_path


def _build_output(pdf_path: str | pathlib.Path, carrierId: int) -> dict[str, object]:
    """Run all parsers and return the consolidated JSON-friendly object."""
    match carrierId:
        case 2:  # Progressive
            return progressive_build_policy_data(pdf_path)
        case _:  # Unsupported carrier
            return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python pdf_converter.py <s3_pdf_url>")
        raise SystemExit(1)

    s3_url = sys.argv[1]
    carrierId = int(sys.argv[2]) if len(sys.argv) > 2 else -1

    if carrierId == -1:
        print(None)
        raise SystemExit(1)

    pdf_tmp_path = _download_pdf_from_s3(s3_url)
    # pdf_tmp_path = "./test1.pdf"  # For local testing without S3

    # output = _build_output(pdf_tmp_path, carrierId=1)

    try:
        output = _build_output(pdf_tmp_path, carrierId)
    finally:
        try:
            os.remove(pdf_tmp_path)
        except OSError:
            pass

    print(json.dumps(output, indent=4))
