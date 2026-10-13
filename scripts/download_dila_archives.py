#!/usr/bin/env python3
"""Download DILA debate archives for specified years."""

import os
import re
import tarfile
import tempfile
from pathlib import Path

import httpx

BASE_URL = "https://echanges.dila.gouv.fr/OPENDATA/Debats/AN"


def list_archives(year: int) -> list:
    """List available .taz archives for a year."""
    url = f"{BASE_URL}/{year}/"
    response = httpx.get(url, timeout=30)
    response.raise_for_status()

    # Extract archive filenames (format: AN_YYYYMMDD_NNN.taz)
    pattern = rf'href="(AN_{year}\d+_\d+\.taz)"'
    archives = re.findall(pattern, response.text)
    return archives


def download_and_extract(year: int, archive: str, output_dir: Path) -> int:
    """Download and extract a .taz archive (various formats)."""
    import subprocess
    import io

    # Skip if marker file indicates this archive was already extracted
    marker = output_dir / f".{archive}.done"
    if marker.exists():
        return 0

    url = f"{BASE_URL}/{year}/{archive}"

    with tempfile.NamedTemporaryFile(suffix=".taz", delete=False) as tmp:
        # Download
        with httpx.stream("GET", url, timeout=60) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                tmp.write(chunk)
        downloaded_path = tmp.name

    count = 0
    try:
        # Check file type
        import subprocess
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
            marker.touch()
            return 0

        # Extract XML files from tar (handle nested tars)
        with tarfile.open(tar_path, "r:") as tar:
            for member in tar.getmembers():
                if member.name.endswith(".xml") and "CRI_" in member.name:
                    # Extract just the filename
                    filename = Path(member.name).name
                    f = tar.extractfile(member)
                    if f:
                        content = f.read()
                        output_path = output_dir / filename
                        output_path.write_bytes(content)
                        count += 1
                elif member.name.endswith(".tar"):
                    # Nested tar archive (2016+ format)
                    f = tar.extractfile(member)
                    if f:
                        inner_tar_data = io.BytesIO(f.read())
                        with tarfile.open(fileobj=inner_tar_data, mode="r:") as inner_tar:
                            for inner_member in inner_tar.getmembers():
                                if inner_member.name.endswith(".xml") and "CRI_" in inner_member.name:
                                    filename = Path(inner_member.name).name
                                    inner_f = inner_tar.extractfile(inner_member)
                                    if inner_f:
                                        content = inner_f.read()
                                        output_path = output_dir / filename
                                        output_path.write_bytes(content)
                                        count += 1
    finally:
        # Cleanup
        for path in [downloaded_path, downloaded_path + ".Z", tar_path if 'tar_path' in dir() else None]:
            if path and os.path.exists(path):
                try:
                    os.unlink(path)
                except:
                    pass

    # Mark archive as done so we skip it next time (even if 0 CRI XML found)
    marker.touch()

    return count


def download_year(year: int, output_dir: Path) -> int:
    """Download all archives for a year."""
    output_dir.mkdir(parents=True, exist_ok=True)

    # Year-level marker for years with no archives available
    year_marker = output_dir / ".year_done"
    if year_marker.exists():
        existing = len(list(output_dir.glob("CRI_*.xml")))
        print(f"Year {year} already processed ({existing} XML files), skipping")
        return 0

    archives = list_archives(year)
    if not archives:
        year_marker.touch()
        return 0

    # Check how many archives are already done
    already_done = sum(1 for a in archives if (output_dir / f".{a}.done").exists())
    if already_done == len(archives):
        existing = len(list(output_dir.glob("CRI_*.xml")))
        print(f"All {len(archives)} archives for {year} already extracted ({existing} XML files), skipping")
        year_marker.touch()
        return 0

    print(f"Found {len(archives)} archives for {year} ({already_done} already done)")

    total = 0
    for i, archive in enumerate(archives):
        count = download_and_extract(year, archive, output_dir)
        total += count
        print(f"  [{i+1}/{len(archives)}] {archive}: {count} XML files")

    year_marker.touch()
    return total


def main():
    years = list(range(2002, 2026))

    for year in years:
        output_dir = Path(f"data/raw/dila/debats_{year}")
        print(f"\n=== Downloading {year} ===")

        try:
            total = download_year(year, output_dir)
            print(f"Total: {total} XML files for {year}")
        except Exception as e:
            print(f"Error downloading {year}: {e}")
            # Mark year as done so we don't retry 404s
            year_marker = output_dir / ".year_done"
            year_marker.touch()


if __name__ == "__main__":
    main()
