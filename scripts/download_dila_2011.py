#!/usr/bin/env python3
"""Download DILA debate archives for 2011 specifically."""

import io
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import httpx

BASE_URL = "https://echanges.dila.gouv.fr/OPENDATA/Debats/AN"


def download_and_extract(year: int, archive: str, output_dir: Path) -> int:
    """Download and extract a .taz archive."""
    url = f"{BASE_URL}/{year}/{archive}"
    print(f"  Downloading {archive}...")

    with tempfile.NamedTemporaryFile(suffix=".taz", delete=False) as tmp:
        # Download
        with httpx.stream("GET", url, timeout=120, follow_redirects=True) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                tmp.write(chunk)
        downloaded_path = tmp.name

    count = 0
    try:
        # Check file type
        result = subprocess.run(["file", downloaded_path], capture_output=True, text=True)
        file_type = result.stdout

        if "compress'd data" in file_type:
            # Unix compress format - decompress first
            compressed_path = downloaded_path + ".Z"
            os.rename(downloaded_path, compressed_path)
            subprocess.run(["uncompress", compressed_path], check=True, capture_output=True)
            tar_path = compressed_path[:-2]
        elif "POSIX tar archive" in file_type:
            # Already a tar archive
            tar_path = downloaded_path
        else:
            print(f"    Unknown file type: {file_type}")
            return 0

        # Extract XML files from tar
        with tarfile.open(tar_path, "r:") as tar:
            for member in tar.getmembers():
                if member.name.endswith(".xml"):
                    # Extract just the filename
                    filename = Path(member.name).name
                    f = tar.extractfile(member)
                    if f:
                        content = f.read()
                        output_path = output_dir / filename
                        output_path.write_bytes(content)
                        count += 1
                elif member.name.endswith(".tar"):
                    # Nested tar archive
                    f = tar.extractfile(member)
                    if f:
                        inner_tar_data = io.BytesIO(f.read())
                        with tarfile.open(fileobj=inner_tar_data, mode="r:") as inner_tar:
                            for inner_member in inner_tar.getmembers():
                                if inner_member.name.endswith(".xml"):
                                    filename = Path(inner_member.name).name
                                    inner_f = inner_tar.extractfile(inner_member)
                                    if inner_f:
                                        content = inner_f.read()
                                        output_path = output_dir / filename
                                        output_path.write_bytes(content)
                                        count += 1
    finally:
        # Cleanup
        for path in [downloaded_path, downloaded_path + ".Z", tar_path if 'tar_path' in locals() else None]:
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except:
                    pass

    return count


def main():
    year = 2011
    output_dir = Path(f"data/raw/dila/debats_{year}")
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Downloading DILA archives for {year}...")

    # List archives
    url = f"{BASE_URL}/{year}/"
    response = httpx.get(url, timeout=30)
    response.raise_for_status()

    # Extract archive filenames - use generic pattern to catch all .taz files
    archives = re.findall(r'href="(AN_[^"]+\.taz)"', response.text)
    print(f"Found {len(archives)} archives for {year}")

    total = 0
    for i, archive in enumerate(archives):
        try:
            count = download_and_extract(year, archive, output_dir)
            total += count
            print(f"  [{i+1}/{len(archives)}] {archive}: {count} XML files")
        except Exception as e:
            print(f"  Error with {archive}: {e}")
            continue

    print(f"\nTotal: {total} XML files for {year}")


if __name__ == "__main__":
    main()
