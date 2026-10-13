#!/usr/bin/env python3
"""Setup Google Document AI for OCR benchmark.

Interactive script that guides through:
1. Installing gcloud CLI (if missing)
2. Authenticating
3. Creating a project (or using existing)
4. Enabling Document AI API
5. Creating an OCR processor
6. Writing config to .env

Usage:
    python scripts/ocr_pipeline/setup_google_docai.py
"""

import os
import subprocess
import sys
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def run(cmd: list[str], check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=capture, text=True, check=check)


def has_gcloud() -> bool:
    try:
        run(["gcloud", "--version"])
        return True
    except FileNotFoundError:
        return False


def get_current_project() -> str | None:
    try:
        r = run(["gcloud", "config", "get-value", "project"])
        val = r.stdout.strip()
        return val if val and val != "(unset)" else None
    except Exception:
        return None


def get_active_account() -> str | None:
    try:
        r = run(["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"])
        return r.stdout.strip() or None
    except Exception:
        return None


def api_enabled(project: str) -> bool:
    try:
        r = run([
            "gcloud", "services", "list", "--enabled",
            "--filter=config.name:documentai.googleapis.com",
            "--project", project,
            "--format=value(config.name)",
        ])
        return "documentai" in r.stdout
    except Exception:
        return False


def list_processors(project: str, location: str) -> list[dict]:
    try:
        r = run([
            "gcloud", "alpha", "document-ai", "processors", "list",
            "--project", project,
            "--location", location,
            "--format=json",
        ])
        import json
        return json.loads(r.stdout) if r.stdout.strip() else []
    except Exception:
        return []


def create_processor(project: str, location: str) -> str | None:
    try:
        r = run([
            "gcloud", "alpha", "document-ai", "processors", "create",
            "--type=OCR_PROCESSOR",
            "--display-name=ocr-benchmark",
            "--project", project,
            "--location", location,
            "--format=value(name)",
        ])
        # name format: projects/PROJECT/locations/LOC/processors/PROCESSOR_ID
        full_name = r.stdout.strip()
        return full_name.split("/")[-1] if full_name else None
    except Exception as e:
        print(f"  Error creating processor: {e}")
        return None


def update_env(key: str, value: str):
    if ENV_FILE.exists():
        lines = ENV_FILE.read_text().splitlines()
    else:
        lines = []

    found = False
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            found = True
            break

    if not found:
        lines.append(f"{key}={value}")

    ENV_FILE.write_text("\n".join(lines) + "\n")


def main():
    print("=" * 60)
    print("Google Document AI — Setup Guide")
    print("=" * 60)

    # Step 1: gcloud CLI
    print("\n[1/5] Checking gcloud CLI...")
    if not has_gcloud():
        print("  gcloud CLI not found.")
        print("  Install: https://cloud.google.com/sdk/docs/install")
        print("  macOS:   brew install google-cloud-sdk")
        sys.exit(1)
    print("  OK")

    # Step 2: Authentication
    print("\n[2/5] Checking authentication...")
    account = get_active_account()
    if not account:
        print("  No active account. Running: gcloud auth login")
        run(["gcloud", "auth", "login"], capture=False, check=False)
        print("  Also setting up application default credentials...")
        run(["gcloud", "auth", "application-default", "login"], capture=False, check=False)
        account = get_active_account()
        if not account:
            print("  Authentication failed.")
            sys.exit(1)
    else:
        # Ensure ADC exists
        adc_path = Path.home() / ".config" / "gcloud" / "application_default_credentials.json"
        if not adc_path.exists():
            print(f"  Logged in as {account}, but no ADC found.")
            print("  Running: gcloud auth application-default login")
            run(["gcloud", "auth", "application-default", "login"], capture=False, check=False)
    print(f"  Authenticated as: {account}")

    # Step 3: Project
    print("\n[3/5] Setting up project...")
    project = get_current_project()
    if project:
        use = input(f"  Use existing project '{project}'? [Y/n] ").strip().lower()
        if use == "n":
            project = input("  Enter project ID: ").strip()
            run(["gcloud", "config", "set", "project", project], check=False)
    else:
        project = input("  Enter Google Cloud project ID: ").strip()
        run(["gcloud", "config", "set", "project", project], check=False)

    if not project:
        print("  No project set.")
        sys.exit(1)
    print(f"  Project: {project}")

    # Step 4: Enable API
    print("\n[4/5] Enabling Document AI API...")
    if api_enabled(project):
        print("  Already enabled")
    else:
        print("  Enabling documentai.googleapis.com...")
        run([
            "gcloud", "services", "enable", "documentai.googleapis.com",
            "--project", project,
        ], capture=False, check=False)
        print("  Done")

    # Step 5: Create processor
    location = "eu"
    print(f"\n[5/5] Setting up OCR processor (location: {location})...")
    processors = list_processors(project, location)
    ocr_processors = [p for p in processors if "OCR" in p.get("type", "")]

    processor_id = None
    if ocr_processors:
        existing = ocr_processors[0]
        existing_id = existing["name"].split("/")[-1]
        print(f"  Found existing OCR processor: {existing_id}")
        use = input("  Use this processor? [Y/n] ").strip().lower()
        if use != "n":
            processor_id = existing_id

    if not processor_id:
        print("  Creating new OCR processor...")
        processor_id = create_processor(project, location)
        if not processor_id:
            print("  Failed to create processor.")
            print("  Create one manually at: https://console.cloud.google.com/ai/document-ai")
            processor_id = input("  Enter processor ID manually: ").strip()

    if not processor_id:
        print("  No processor ID. Aborting.")
        sys.exit(1)

    print(f"  Processor ID: {processor_id}")

    # Write to .env
    print("\n--- Writing config to .env ---")
    update_env("GOOGLE_CLOUD_PROJECT", project)
    update_env("DOCAI_LOCATION", location)
    update_env("DOCAI_PROCESSOR_ID", processor_id)
    print(f"  GOOGLE_CLOUD_PROJECT={project}")
    print(f"  DOCAI_LOCATION={location}")
    print(f"  DOCAI_PROCESSOR_ID={processor_id}")

    print("\n" + "=" * 60)
    print("Setup complete!")
    print("=" * 60)
    print("\nInstall the dependency:")
    print('  pip install -e ".[google-ocr]"')
    print("\nRun the benchmark:")
    print("  python scripts/ocr_pipeline/ocr_engine_shootout.py --models google-docai")


if __name__ == "__main__":
    main()
