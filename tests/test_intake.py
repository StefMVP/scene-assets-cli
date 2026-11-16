"""Replay GLB/glTF intake, import and the candidate sheet with generated files. No live API."""
import argparse
import base64
import io
import json
from pathlib import Path
import shutil
import struct
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scene_assets import cli as assets

CLI = [sys.executable, "-m", "scene_assets"]
BLENDER = shutil.which("blender")
# A 1x1 PNG.
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFBQIAX8jx0gAAAABJRU5ErkJggg==")


def triangle_doc(buffer_uri=None, image=None):
    positions = struct.pack("<9f", 0, 0, 0, 2, 0, 0, 0, 1, 0.5)
    doc = {"asset": {"version": "2.0", "generator": "test"},
           "buffers": [{"byteLength": len(positions), **({"uri": buffer_uri} if buffer_uri else {})}],
           "bufferViews": [{"buffer": 0, "byteLength": len(positions)}],
           "accessors": [{"bufferView": 0, "componentType": 5126, "count": 3, "type": "VEC3",
                          "min": [0, 0, 0], "max": [2, 1, 0.5]}],
           "meshes": [{"primitives": [{"attributes": {"POSITION": 0}}]}],
           "nodes": [{"mesh": 0, "name": "Tri"}], "scenes": [{"nodes": [0]}], "scene": 0}
    if image:
        doc["images"] = [{"uri": image}]
    return doc, positions


def glb(path):
    doc, positions = triangle_doc()
    text = json.dumps(doc).encode()
    text += b" " * (-len(text) % 4)
    body = struct.pack("<II", len(text), 0x4E4F534A) + text + struct.pack("<II", len(positions), 0x004E4942) + positions
    path.write_bytes(b"glTF" + struct.pack("<II", 2, 12 + len(body)) + body)
    return path


def gltf(folder):
    doc, positions = triangle_doc("tri.bin", "tex/color.png")
    (folder / "tex").mkdir(parents=True)
    (folder / "tri.bin").write_bytes(positions)
    (folder / "tex/color.png").write_bytes(PNG)
    path = folder / "tri.gltf"
    path.write_text(json.dumps(doc))
    return path


def intake(source, dest, **extra):
    values = dict(source=str(source), dest=dest, kind=None, asset_id=None, name=None, provider=None,
                  source_url=None, license=None, author=None, use="reused", dep=None, max_mb=512)
    values.update(extra)
    return assets.intake(argparse.Namespace(**values))


def test_glb_intake_pins_actual_bytes_and_facts_without_license(tmp_path):
    source = glb(tmp_path / "Tri.glb")
    result = intake(source, tmp_path / "cache")
    manifest = json.loads((tmp_path / "cache/manifest.json").read_text())
    assert manifest["license"] == "unspecified" and manifest["kind"] == "model" and manifest["format"] == "glb"
    assert manifest["files"] == [{"path": "Tri.glb", "source": str(source), "size": source.stat().st_size,
                                  "sha256": assets.digest(source)}]
    assert result["source_facts"]["gltf"]["mesh_bounds"]["size"] == [2, 1, 0.5]
    # The pinned cache is a normal fetch cache: a rerun is offline and hash-checked.
    source.unlink()
    assets.fetch(argparse.Namespace(manifest=tmp_path / "cache/manifest.json", dest=tmp_path / "cache",
                                    asset_id=None, resolution="source", format=None, use="reused"))
    with pytest.raises(ValueError, match="already holds"):
        intake(glb(tmp_path / "Tri.glb"), tmp_path / "cache")


def test_gltf_dependencies_are_packaged_and_https_is_pinned_as_url(tmp_path, monkeypatch):
    folder = gltf(tmp_path / "src").parent
    served = {}

    def fake_open(source):
        if source.startswith("https://"):
            served.setdefault("urls", []).append(source)
            return io.BytesIO((folder / source.rsplit("/src/", 1)[1]).read_bytes())
        return open(source, "rb")

    monkeypatch.setattr(assets, "open_source", fake_open)
    intake("https://example.invalid/lib/src/tri.gltf", tmp_path / "cache", license="CC0-1.0")
    manifest = json.loads((tmp_path / "cache/manifest.json").read_text())
    assert {f["path"]: f["url"] for f in manifest["files"]} == {
        "tex/color.png": "https://example.invalid/lib/src/tex/color.png",
        "tri.bin": "https://example.invalid/lib/src/tri.bin",
        "tri.gltf": "https://example.invalid/lib/src/tri.gltf"}
    assert manifest["provider"] == "example.invalid" and manifest["license"] == "CC0-1.0"
    assert (tmp_path / "cache/tex/color.png").read_bytes() == PNG


def test_missing_dependency_and_corrupt_glb_leave_no_cache_files(tmp_path):
    source = gltf(tmp_path / "src")
    (tmp_path / "src/tri.bin").unlink()
    with pytest.raises(ValueError, match="tri.bin could not be packaged"):
        intake(source, tmp_path / "missing")
    assert list((tmp_path / "missing").iterdir()) == []
    data = glb(tmp_path / "Tri.glb").read_bytes()
    for name, body in (("Short.glb", data[:-4]), ("Fake.glb", b"NOPE" + data[4:]), ("Empty.glb", b"")):
        (tmp_path / name).write_bytes(body)
        with pytest.raises(ValueError):
            intake(tmp_path / name, tmp_path / name.lower())
        assert list((tmp_path / name.lower()).iterdir()) == []


def test_size_limit_and_unsafe_dependency_are_refused(tmp_path):
    with pytest.raises(ValueError, match="intake limit"):
        intake(glb(tmp_path / "Tri.glb"), tmp_path / "big", max_mb=0)
    doc, _ = triangle_doc("../outside.bin")
    (tmp_path / "evil.gltf").write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="Unsafe"):
        intake(tmp_path / "evil.gltf", tmp_path / "evil")
    with pytest.raises(ValueError, match="https"):
        intake("http://example.invalid/a.glb", tmp_path / "plain")


def test_manifest_with_undeclared_gltf_dependency_fails_verify(tmp_path):
    intake(gltf(tmp_path / "src"), tmp_path / "cache")
    manifest = json.loads((tmp_path / "cache/manifest.json").read_text())
    manifest["files"] = [f for f in manifest["files"] if f["path"] != "tri.bin"]
    with pytest.raises(ValueError, match="Missing glTF dependency"):
        assets.verify(manifest, tmp_path / "cache")


def test_sheet_lists_candidates_with_source_metadata(tmp_path):
    intake(gltf(tmp_path / "src"), tmp_path / "a", license="CC0-1.0", author=["Artist"])
    intake(glb(tmp_path / "Tri.glb"), tmp_path / "b")
    output = tmp_path / "sheet.svg"
    result = subprocess.run(CLI + ["sheet", str(tmp_path / "a"), str(tmp_path / "b/manifest.json"), "--output", str(output)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert [c["asset_id"] for c in data["candidates"]] == ["tri", "Tri"]
    assert data["candidates"][0]["preview"] == "embedded texture, not a model view"
    assert data["candidates"][1]["preview"] is None
    svg = output.read_text()
    assert "license: CC0-1.0" in svg and "authors: Artist" in svg and "2 x 1 x 0.5 m raw mesh" in svg
    assert subprocess.run(CLI + ["sheet", str(tmp_path / "a"), "--output", str(output)],
                          capture_output=True, timeout=30).returncode == 1
    (tmp_path / "a/tri.bin").write_bytes(b"x")
    assert subprocess.run(CLI + ["sheet", str(tmp_path / "a"), "--output", str(tmp_path / "s2.svg")],
                          capture_output=True, timeout=30).returncode == 1


@pytest.mark.skipif(not shutil.which("rsvg-convert"), reason="rsvg-convert is not installed")
def test_sheet_png(tmp_path):
    intake(glb(tmp_path / "Tri.glb"), tmp_path / "b")
    output = tmp_path / "sheet.png"
    assert subprocess.run(CLI + ["sheet", str(tmp_path / "b"), "--output", str(output)], timeout=30).returncode == 0
    assert output.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.skipif(not BLENDER, reason="Blender is not installed")
@pytest.mark.parametrize("kind", ["glb", "gltf"])
def test_actual_blender_imports_glb_and_gltf(tmp_path, kind):
    source = glb(tmp_path / "Tri.glb") if kind == "glb" else gltf(tmp_path / "src")
    intake(source, tmp_path / "cache")
    output = tmp_path / "scene.blend"
    result = subprocess.run(CLI + ["import", str(tmp_path / "cache/manifest.json"), "--output", str(output)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    facts = json.loads(Path(str(output) + ".assets.json").read_text())[0]
    mesh = next(o for o in facts["objects"] if o["type"] == "MESH")
    assert [round(v, 3) for v in mesh["dimensions"]] == [2, 0.5, 1]  # glTF Y-up becomes Blender Z-up.
    assert facts["unit_handling"].startswith("glTF units are meters")
    if kind == "gltf":
        # A removed dependency fails before Blender runs.
        (tmp_path / "cache/tri.bin").unlink()
        result = subprocess.run(CLI + ["import", str(tmp_path / "cache/manifest.json"), "--output", str(tmp_path / "x.blend")],
                                capture_output=True, text=True, timeout=60)
        assert result.returncode == 1 and "Missing file" in result.stderr
