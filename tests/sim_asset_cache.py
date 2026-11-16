"""Run the real cache through interruption, repair and corruption with deterministic I/O."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import sys
import tempfile
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scene_assets import cli as assets


def event(kind, **values):
    print(json.dumps({"event": kind, **values}, sort_keys=True))


def check(condition, name):
    if not condition:
        event("invariant", name=name)
        raise SystemExit(1)


def run(seed):
    rng = random.Random(seed)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        data = bytes(rng.getrandbits(8) for _ in range(128))
        source = root / "source.blend"
        source.write_bytes(data)
        spec = {"version": 1, "provider": "Simulation", "asset_id": "floor", "kind": "material",
                "source_url": "https://example.invalid/floor", "license": "CC0-1.0", "resolution": "1k",
                "format": "blend", "use": "reused", "entrypoint": "floor.blend",
                "files": [{"path": "floor.blend", "source": str(source), "size": len(data),
                           "sha256": hashlib.sha256(data).hexdigest()}]}
        plan = root / "source.json"
        plan.write_text(json.dumps(spec))
        dest = root / "cache"
        args = SimpleNamespace(dest=dest, manifest=plan, asset_id=None, resolution="1k", format=None, use="reused")
        original_download = assets.download
        def interrupt(*_):
            raise OSError("injected interruption")
        assets.download = interrupt
        try:
            assets.fetch(args)
        except OSError:
            pass
        finally:
            assets.download = original_download
        check((dest / "manifest.json").exists(), "interruption-keeps-pin")
        check(not (dest / ".asset-lock").exists(), "failure-releases-lock")
        assets.fetch(args)
        pinned = (dest / "manifest.json").read_bytes()
        cached = dest / "floor.blend"
        event("resumed", sha256=assets.digest(cached))
        cached.unlink()
        assets.fetch(args)
        check(cached.read_bytes() == data, "missing-file-restored")
        source.unlink()
        assets.fetch(args)
        check((dest / "manifest.json").read_bytes() == pinned, "offline-rerun-preserves-pin")
        bad = bytearray(data)
        bad[rng.randrange(len(bad))] ^= 1
        cached.write_bytes(bad)
        refused = False
        try:
            assets.fetch(args)
        except ValueError:
            refused = True
        check(refused, "corruption-is-refused")
        check(cached.read_bytes() == bad, "unique-file-preserved")
        check((dest / "manifest.json").read_bytes() == pinned, "corruption-cannot-repin")
        event("passed", seed=seed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, required=True)
    run(parser.parse_args().seed)
