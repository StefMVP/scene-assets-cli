"""Exercise cache publication, transport, and output boundaries."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scene_assets import cli as assets


def intake_args(source, dest, deps=None):
    return argparse.Namespace(source=str(source), dest=dest, kind=None, asset_id=None,
                              name=None, provider=None, source_url=None, license=None,
                              author=None, use="reused", dep=deps, max_mb=1)


def test_failed_intake_rolls_back_only_new_files(tmp_path, monkeypatch):
    source = tmp_path / "model.blend"
    source.write_bytes(b"trusted test data")
    dep = tmp_path / "texture.png"
    dep.write_bytes(b"dependency")
    dest = tmp_path / "cache"
    dest.mkdir()
    (dest / "keep.txt").write_text("unrelated")
    original = assets.os.link
    calls = []

    def fail_second(src, dst):
        calls.append(dst)
        if len(calls) == 2:
            raise OSError("injected publication failure")
        return original(src, dst)

    monkeypatch.setattr(assets.os, "link", fail_second)
    with pytest.raises(OSError, match="publication failure"):
        assets.intake(intake_args(source, dest, [str(dep)]))
    assert sorted(p.name for p in dest.iterdir()) == ["keep.txt"]
    assert (dest / "keep.txt").read_text() == "unrelated"


def test_invalid_gltf_leaves_no_published_files(tmp_path):
    source = tmp_path / "bad.gltf"
    source.write_text(json.dumps({"asset": {"version": "2.0"}, "meshes": None}))
    dest = tmp_path / "cache"
    with pytest.raises(ValueError, match="glTF"):
        assets.intake(intake_args(source, dest))
    assert list(dest.iterdir()) == []


def test_fetch_refuses_https_downgrade(tmp_path, monkeypatch):
    data = b"asset"

    class Response(io.BytesIO):
        def geturl(self):
            return "http://example.invalid/asset.blend"

    monkeypatch.setattr(assets, "urlopen", lambda *a, **k: Response(data))
    record = {"path": "asset.blend", "url": "https://example.invalid/asset.blend",
              "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    with pytest.raises(ValueError, match="HTTPS"):
        assets.download(tmp_path, record)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("link_target_exists", [True, False])
def test_import_refuses_receipt_symlink_before_blender(tmp_path, monkeypatch, link_target_exists):
    source = tmp_path / "asset.blend"
    source.write_bytes(b"trusted test data")
    assets.intake(intake_args(source, tmp_path / "cache"))
    output = tmp_path / "scene.blend"
    elsewhere = tmp_path / "unrelated.txt"
    if link_target_exists:
        elsewhere.write_text("keep")
    receipt = Path(str(output) + ".assets.json")
    receipt.symlink_to(elsewhere)
    monkeypatch.setattr(assets.subprocess, "run", lambda *a, **k: pytest.fail("Blender must not run"))
    args = argparse.Namespace(manifest=tmp_path / "cache/manifest.json", output=output,
                              scene=None, object=None, blender="blender", timeout=30)
    with pytest.raises(ValueError, match="receipt"):
        assets.import_asset(args)
    assert receipt.is_symlink()
    assert not output.exists()
    assert elsewhere.read_text() == "keep" if link_target_exists else not elsewhere.exists()


def test_sheet_with_long_xml_characters_is_valid(tmp_path):
    from xml.etree import ElementTree
    source = tmp_path / "asset.blend"
    source.write_bytes(b"trusted test data")
    args = intake_args(source, tmp_path / "cache")
    args.asset_id = "&" * 50
    assets.intake(args)
    output = tmp_path / "sheet.svg"
    assets.sheet(argparse.Namespace(candidates=[args.dest], output=output))
    ElementTree.parse(output)


@pytest.mark.parametrize("url", ["http://example.invalid/a", "file:///tmp/a", "https://user:pass@example.invalid/a"])
def test_redirect_is_refused_before_opening_destination(url):
    from urllib.request import Request
    handler = assets.HTTPSRedirect()
    with pytest.raises(ValueError, match="HTTPS"):
        handler.redirect_request(Request("https://example.invalid/start"), None, 302, "Found", {}, url)


def test_https_redirect_remains_supported():
    from urllib.request import Request
    request = assets.HTTPSRedirect().redirect_request(
        Request("https://example.invalid/start"), None, 302, "Found", {}, "https://cdn.example.invalid/a")
    assert request.full_url == "https://cdn.example.invalid/a"


def test_failed_nested_publication_removes_empty_new_directories(tmp_path, monkeypatch):
    source = tmp_path / "model.blend"
    source.write_bytes(b"trusted test data")
    dep = tmp_path / "texture.png"
    dep.write_bytes(b"dependency")
    dest = tmp_path / "cache"
    original = assets.os.link

    def fail_entry(src, dst):
        if dst.name == "model.blend":
            raise OSError("injected publication failure")
        return original(src, dst)

    monkeypatch.setattr(assets.os, "link", fail_entry)
    with pytest.raises(OSError, match="publication failure"):
        assets.intake(intake_args(source, dest, ["a/b/texture.png=" + str(dep)]))
    assert list(dest.iterdir()) == []


def test_concurrent_imports_own_the_output_until_receipt_is_saved(tmp_path):
    import os
    source = tmp_path / "asset.blend"
    source.write_bytes(b"trusted test data")
    cache = tmp_path / "cache"
    assets.intake(intake_args(source, cache))
    backend = tmp_path / "blender-stand-in"
    backend.write_text("#!" + sys.executable + "\n" + '''import json,os,select,sys
from pathlib import Path
manifest, output, obj = sys.argv[sys.argv.index("--") + 1:]
output = Path(output)
receipt = Path(str(output) + ".assets.json")
if output.exists() or receipt.exists():
    raise SystemExit(1)
role = os.environ["IMPORT_ROLE"]
if role == "first":
    print("SAVE_BARRIER", flush=True)
    if not select.select([sys.stdin], [], [], 10)[0]:
        raise SystemExit(2)
    sys.stdin.readline()
output.write_text(role)
with receipt.open("x") as stream:
    json.dump({"owner": role}, stream)
''')
    backend.chmod(0o700)
    output = tmp_path / "shared.blend"
    command = [sys.executable, "-m", "scene_assets", "import", str(cache / "manifest.json"),
               "--output", str(output), "--blender", str(backend)]
    first = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env={**os.environ, "IMPORT_ROLE": "first"})
    try:
        assert first.stderr.readline().strip() == "SAVE_BARRIER"
        second = subprocess.run(command, capture_output=True, text=True, timeout=5,
                                env={**os.environ, "IMPORT_ROLE": "second"})
    finally:
        stdout, stderr = first.communicate("\n", timeout=15)
    assert second.returncode == 1, second.stdout + second.stderr
    assert "locked" in second.stderr.lower()
    assert first.returncode == 0, stdout + stderr
    assert json.loads(stdout)["scene"] == str(output)
    assert output.read_text() == "first"
    assert json.loads(Path(str(output) + ".assets.json").read_text()) == {"owner": "first"}
    assert not Path(str(output) + ".asset-lock").exists()
