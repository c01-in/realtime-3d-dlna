"""Install runtime assets from their upstream releases."""

import argparse
import hashlib
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

THRU = "f04e925f5fc85b452be4387b777561e8899518bd"
OVDA = "4173c24e22f528062a0cf8ae235c58a5d4376a74"
WEIGHT_SHA256 = "ea531d53a1135a443bafcf106a175b26a6519c05ccc1e994d74e46159e14b7cb"
WEIGHT_URL = "https://huggingface.co/FriedFeid/oVDA/resolve/9eda225/oVDA_c16.pth"
THRU_FILES = {"LICENSE", "config.py", "media_library.py", "offline/__init__.py",
              "offline/two_dvr_gpu.py", "offline/two_dvr_render.py"}


def download(url, target):
    with urllib.request.urlopen(url, timeout=90) as source, target.open("wb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)


def install_source(root, name, repo, commit, include):
    dest = root / name
    if dest.is_dir():
        return
    with tempfile.TemporaryDirectory(dir=root) as temp:
        folder = Path(temp)
        archive = folder / "source.zip"
        print(f"Fetching {repo} at {commit}", flush=True)
        download(f"https://codeload.github.com/{repo}/zip/{commit}", archive)
        checkout = folder / "source"
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                path = PurePosixPath(member.filename)
                if member.is_dir() or len(path.parts) < 2 or ".." in path.parts:
                    continue
                relative = path.relative_to(path.parts[0])
                if not include(relative.as_posix()):
                    continue
                target = checkout.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
        checkout.rename(dest)


def install_assets(data_dir, model_dir):
    sources = data_dir / "upstream"
    sources.mkdir(parents=True, exist_ok=True)
    model_dir.mkdir(parents=True, exist_ok=True)
    install_source(sources, "thru3d", "zerochocobo/Thru3D-Media-Server", THRU,
                   lambda path: path in THRU_FILES or
                   path.startswith(("dlna/", "utils/")) and path.endswith(".py"))
    install_source(sources, "ovda", "FriedFeid/OnlineVideoDepthAnything", OVDA,
                   lambda path: path in {"LICENSE", "configs/oVDA_c16.yaml"} or
                   path.startswith("src/models/") and path.endswith(".py"))
    weight = model_dir / "oVDA_c16.pth"
    if weight.is_file():
        with weight.open("rb") as existing:
            if hashlib.file_digest(existing, "sha256").hexdigest() == WEIGHT_SHA256:
                return
    with tempfile.TemporaryDirectory(dir=model_dir) as temp:
        candidate = Path(temp) / weight.name
        print("Fetching oVDA c16 weights from the author's release", flush=True)
        download(WEIGHT_URL, candidate)
        with candidate.open("rb") as file:
            if hashlib.file_digest(file, "sha256").hexdigest() != WEIGHT_SHA256:
                raise RuntimeError("oVDA checkpoint checksum mismatch")
        candidate.replace(weight)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--model-dir", type=Path)
    parser.add_argument("--accept-ovda-noncommercial", action="store_true", required=True,
                        help="Review and accept the upstream NC-SA-UHDV1.0 terms for oVDA.")
    args = parser.parse_args()
    install_assets(args.data_dir, args.model_dir or args.data_dir / "models")
