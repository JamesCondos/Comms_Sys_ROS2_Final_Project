"""Write or verify a relative-path SHA-256 manifest for a retained E1 dataset."""
import argparse
import hashlib
import json
from pathlib import Path


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("results", type=Path)
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    root = args.results.resolve()
    manifest = root / "sha256_manifest.json"
    if args.verify:
        records = json.loads(manifest.read_text())
        for name, expected in records.items():
            target = (root / name).resolve()
            if not target.is_relative_to(root):
                raise ValueError("Manifest path escapes dataset")
            if not target.is_file() or sha256(target) != expected:
                raise ValueError(f"Missing or changed dataset file: {name}")
        print(f"Verified {len(records)} retained files against SHA-256 manifest.")
    else:
        if manifest.exists():
            raise FileExistsError("Refusing to overwrite an existing provenance manifest")
        records = {str(p.relative_to(root)).replace("\\", "/"): sha256(p)
                   for p in sorted(root.rglob("*")) if p.is_file() and p != manifest}
        manifest.write_text(json.dumps(records, indent=2) + "\n")
        print(f"Recorded SHA-256 for {len(records)} retained files.")


if __name__ == "__main__":
    main()
