"""Upload this project directly to its Hugging Face Docker Space.

The script uses the authenticated ``hf`` CLI. It uploads only the files needed
to build and run the Space, so local settings, caches, and development files
cannot accidentally become part of a deployment.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REPO_ID = "kaushikpaul/Dlp-Video-Downloader"
ROOT_FILES = {
    ".dockerignore",
    "Dockerfile",
    "LICENCE",
    "README.md",
    "requirements.txt",
}
IGNORED_PARTS = {
    ".agents",
    ".codex",
    ".git",
    ".idea",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "venv",
}


def is_safe_deploy_file(path: Path) -> bool:
    relative = path.relative_to(PROJECT_ROOT)
    if path.is_symlink() or not path.is_file():
        return False
    if any(part in IGNORED_PARTS for part in relative.parts):
        return False
    if any(part == ".env" or part.startswith(".env.") for part in relative.parts):
        return False
    if path.suffix in {".pyc", ".pyo"}:
        return False
    return relative.as_posix() in ROOT_FILES or relative.parts[0] == "main"


def deploy_files() -> list[str]:
    candidates = [PROJECT_ROOT / name for name in ROOT_FILES]
    candidates.extend((PROJECT_ROOT / "main").rglob("*"))
    return sorted(
        path.relative_to(PROJECT_ROOT).as_posix()
        for path in candidates
        if is_safe_deploy_file(path)
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Upload the yt-dlp media server to Hugging Face Spaces."
    )
    parser.add_argument(
        "--repo-id",
        default=DEFAULT_REPO_ID,
        help=f"Target Space ID (default: {DEFAULT_REPO_ID}).",
    )
    parser.add_argument(
        "--commit-message",
        default="Deploy YT-DLP Media Server",
        help="Commit message recorded by Hugging Face for this upload.",
    )
    parser.add_argument(
        "--create",
        action="store_true",
        help="Create the protected Docker Space first if it does not exist.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the upload set without contacting Hugging Face.",
    )
    return parser.parse_args()


def run_checked(command: list[str], *, error_message: str) -> None:
    try:
        subprocess.run(command, cwd=PROJECT_ROOT, check=True)
    except subprocess.CalledProcessError as exc:
        raise SystemExit(error_message) from exc


def main() -> None:
    args = parse_args()
    files = deploy_files()
    upload_bytes = sum((PROJECT_ROOT / path).stat().st_size for path in files)

    print(f"Target: https://huggingface.co/spaces/{args.repo_id}")
    print(f"Upload set: {len(files)} files, {upload_bytes / 1024:.1f} KiB")
    if args.dry_run:
        for path in files:
            print(path)
        return

    hf_command = shutil.which("hf")
    if not hf_command:
        raise SystemExit(
            "The hf CLI is not installed. Install it, run `hf auth login`, then retry."
        )

    run_checked(
        [hf_command, "auth", "whoami"],
        error_message="Hugging Face authentication failed. Run `hf auth login` and retry.",
    )

    if args.create:
        run_checked(
            [
                hf_command,
                "repos",
                "create",
                args.repo_id,
                "--type",
                "space",
                "--sdk",
                "docker",
                "--protected",
                "--exist-ok",
            ],
            error_message="Could not create or access the target Docker Space.",
        )

    command = [
        hf_command,
        "upload",
        args.repo_id,
        ".",
        ".",
        "--repo-type",
        "space",
        "--commit-message",
        args.commit_message,
    ]
    for path in files:
        command.extend(("--include", path))

    print("Uploading current local files (no Git commit or push required)...")
    run_checked(
        command,
        error_message="Deployment failed. Check the Space ID and your write access.",
    )
    print(f"Deployment submitted: https://huggingface.co/spaces/{args.repo_id}")


if __name__ == "__main__":
    main()
