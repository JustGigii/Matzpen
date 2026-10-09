#!/usr/bin/env python3
import argparse
import shutil
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a deployment bundle with only the runtime-relevant files."
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("deployment"),
        help="Target folder for the deployment bundle (default: deployment).",
    )
    return parser.parse_args()


def copy_item(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst)


def create_bundle(output_dir: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    target_dir = output_dir if output_dir.is_absolute() else repo_root / output_dir

    if target_dir.exists():
        shutil.rmtree(target_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    items = [
        (repo_root / "pyproject.toml", target_dir / "pyproject.toml"),
        (repo_root / "requirements.txt", target_dir / "requirements.txt"),
        (repo_root / "README.md", target_dir / "README.md"),
        (repo_root / "alembic.ini", target_dir / "alembic.ini"),
        (repo_root / "Dockerfile", target_dir / "Dockerfile"),
        (repo_root / "migrations", target_dir / "migrations"),
        (repo_root / "secrets", target_dir / "secrets"),
        (repo_root / "src" / "personal_agent", target_dir / "src" / "personal_agent"),
        (repo_root / "deploy" / "aapanel-nginx.conf", target_dir / "deploy" / "aapanel-nginx.conf"),
    ]

    for src, dst in items:
        if src.exists():
            copy_item(src, dst)
            print(f"Copied: {src.relative_to(repo_root)}")
        else:
            print(f"Skipped missing file: {src.relative_to(repo_root)}")

    instructions = """Deployment bundle created.

Next steps:
1. cd into this folder
2. pip install .
3. alembic upgrade head
4. uvicorn personal_agent.main:app --host 0.0.0.0 --port 8000
"""
    (target_dir / "README_DEPLOYMENT.txt").write_text(instructions, encoding="utf-8")

    print(f"\nDeployment bundle ready at: {target_dir}")


def main() -> None:
    args = parse_args()
    create_bundle(args.output)


if __name__ == "__main__":
    main()
