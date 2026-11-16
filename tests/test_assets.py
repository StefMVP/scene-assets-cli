"""Replay asset selection, cache integrity and Blender failure boundaries without live APIs."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scene_assets import cli as assets


def manifest(source, kind="material"):
    return {"version": 1, "provider": "Test library", "asset_id": "floor", "kind": kind,
            "source_url": "https://example.invalid/floor", "license": "CC0-1.0", "authors": {},
            "resolution": "1k", "format": "blend", "use": "reused", "entrypoint": "floor.blend",
            "files": [{"path": "floor.blend", "source": str(source), "size": source.stat().st_size,
                       "sha256": assets.digest(source)}]}


def args(source, dest):
    return argparse.Namespace(manifest=source, dest=dest, asset_id=None, resolution="1k", format=None, use="reused")


def test_cli_routes_assets_locally():
    result = subprocess.run([sys.executable, "-m", "scene_assets", "--help"],
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0
    assert "search" in result.stdout and "fetch" in result.stdout and "import" in result.stdout


def test_rerun_is_offline_hash_checked_and_preserves_unique_files(tmp_path, monkeypatch):
    source = tmp_path / "source.blend"
    source.write_bytes(b"pinned library data")
    plan = tmp_path / "source.json"
    plan.write_text(json.dumps(manifest(source)))
    dest = tmp_path / "cache"
    assets.fetch(args(plan, dest))
    pinned = (dest / "manifest.json").read_bytes()
    unique = dest / "unique.txt"
    unique.write_text("keep")
    source.unlink()
    monkeypatch.setattr(assets, "api", lambda _: pytest.fail("Cache rerun called API"))
    assets.fetch(args(plan, dest))
    assert (dest / "manifest.json").read_bytes() == pinned
    assert unique.read_text() == "keep"
    cached = dest / "floor.blend"
    cached.write_bytes(b"changed library dat")
    with pytest.raises(ValueError, match="mismatch"):
        assets.fetch(args(plan, dest))
    assert cached.read_bytes() == b"changed library dat"
    assert (dest / "manifest.json").read_bytes() == pinned


def test_missing_file_resumes_from_pinned_source(tmp_path):
    source = tmp_path / "source.blend"
    source.write_bytes(b"source")
    plan = tmp_path / "source.json"
    plan.write_text(json.dumps(manifest(source)))
    dest = tmp_path / "cache"
    assets.fetch(args(plan, dest))
    (dest / "floor.blend").unlink()
    assets.fetch(args(plan, dest))
    assert (dest / "floor.blend").read_bytes() == source.read_bytes()


def test_bundle_includes_all_dependencies_and_pins_provider_facts(monkeypatch):
    info = {"type": 2, "name": "Bed", "authors": {"Artist": "All"}, "dimensions": [1, 2]}
    bundle = {"url": "https://example.invalid/bed.blend", "md5": "a" * 32, "size": 8,
              "include": {"textures/bed.jpg": {"url": "https://example.invalid/bed.jpg", "md5": "b" * 32, "size": 9},
                          "textures/bed_nor.exr": {"url": "https://example.invalid/bed.exr", "md5": "c" * 32, "size": 10}}}
    monkeypatch.setattr(assets, "api", lambda path: info if path.startswith("/info/") else {"blend": {"2k": {"blend": bundle}}})
    pinned = assets.plan("bed", "2k", "blend", "adapted")
    assert {f["path"] for f in pinned["files"]} == {"bed.blend", "textures/bed.jpg", "textures/bed_nor.exr"}
    assert pinned["authors"] == info["authors"] and pinned["source_facts"]["dimensions"] == [1, 2]
    assert pinned["use"] == "adapted"
    with pytest.raises(ValueError, match="No 4k"):
        assets.plan("bed", "4k", "blend", "reused")


@pytest.mark.parametrize("path", ["../escape.blend", "/escape.blend", "textures/../../escape", "a\\b", "C:/bad", "a//b", "a/./b", "manifest.json", ".asset-lock"])
def test_reject_unsafe_paths_before_any_download(tmp_path, path):
    source = tmp_path / "source.blend"
    source.write_bytes(b"source")
    data = manifest(source)
    data["files"][0]["path"] = path
    with pytest.raises(ValueError):
        assets.validate(data)


def test_missing_entrypoint_duplicate_and_symlink_are_refused(tmp_path):
    source = tmp_path / "source.blend"
    source.write_bytes(b"source")
    data = manifest(source)
    data["entrypoint"] = "missing.blend"
    with pytest.raises(ValueError, match="entrypoint"):
        assets.validate(data)
    data = manifest(source)
    data["files"].append(deepcopy(data["files"][0]))
    with pytest.raises(ValueError, match="Duplicate"):
        assets.validate(data)
    root = tmp_path / "cache"
    root.mkdir()
    (root / "floor.blend").symlink_to(source)
    with pytest.raises(ValueError, match="symlink"):
        assets.verify(manifest(source), root)


def test_wrong_download_is_never_published_and_lock_is_released(tmp_path):
    source = tmp_path / "source.blend"
    source.write_bytes(b"source")
    data = manifest(source)
    data["files"][0]["sha256"] = "0" * 64
    plan = tmp_path / "source.json"
    plan.write_text(json.dumps(data))
    dest = tmp_path / "cache"
    with pytest.raises(ValueError, match="sha256 mismatch"):
        assets.fetch(args(plan, dest))
    assert not (dest / "floor.blend").exists()
    assert not (dest / ".asset-lock").exists()
    assert not list(dest.glob(".asset-*"))


def test_import_refuses_missing_dependencies_before_blender(tmp_path, monkeypatch):
    source = tmp_path / "source.blend"
    source.write_bytes(b"source")
    plan = tmp_path / "manifest.json"
    plan.write_text(json.dumps(manifest(source)))
    monkeypatch.setattr(assets.subprocess, "run", lambda *a, **k: pytest.fail("Blender was called"))
    with pytest.raises(ValueError, match="Missing file"):
        assets.import_asset(argparse.Namespace(manifest=plan))


BLENDER = shutil.which("blender")


@pytest.mark.skipif(not BLENDER, reason="Blender is not installed")
def test_actual_blender_rejects_invalid_bundle(tmp_path):
    source = tmp_path / "floor.blend"
    source.write_bytes(b"invalid blend")
    plan = tmp_path / "manifest.json"
    plan.write_text(json.dumps(manifest(source)))
    output = tmp_path / "out.blend"
    result = subprocess.run([sys.executable, "-m", "scene_assets", "import", str(plan),
                             "--output", str(output), "--blender", BLENDER], capture_output=True, text=True, timeout=40)
    assert result.returncode == 1
    assert not output.exists()


@pytest.mark.skipif(not BLENDER, reason="Blender is not installed")
def test_actual_blender_preserves_material_and_packs_images(tmp_path):
    source = tmp_path / "floor.blend"
    make = tmp_path / "make.py"
    make.write_text('''import bpy
from pathlib import Path
bpy.ops.wm.read_factory_settings(use_empty=True)
m = bpy.data.materials.new("Library floor")
m.use_nodes = True
m.use_fake_user = True
image = bpy.data.images.new("source color", width=2, height=2)
image.pack()
node = m.node_tree.nodes.new("ShaderNodeTexImage")
node.image = image
m.node_tree.links.new(node.outputs["Color"], m.node_tree.nodes.get("Principled BSDF").inputs["Base Color"])
bpy.ops.wm.save_as_mainfile(filepath=''' + repr(str(source)) + ''')
''')
    subprocess.run([BLENDER, "-b", "--factory-startup", "--python-exit-code", "1", "--python", str(make)],
                   check=True, capture_output=True, timeout=40)
    plan = tmp_path / "manifest.json"
    plan.write_text(json.dumps(manifest(source)))
    output = tmp_path / "imported.blend"
    command = [sys.executable, "-m", "scene_assets", "import", str(plan), "--output", str(output)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    facts = json.loads(Path(str(output) + ".assets.json").read_text())[0]
    assert facts["materials"][0]["name"] == "Library floor"
    assert facts["materials"][0]["links"] == 2
    assert output.is_file()
    before = assets.digest(output)
    assert subprocess.run(command, capture_output=True, timeout=10).returncode == 1
    assert assets.digest(output) == before


@pytest.mark.skipif(not BLENDER, reason="Blender is not installed")
def test_actual_blender_requires_linked_mesh_dependencies(tmp_path):
    dependency = tmp_path / "mesh-dependency.blend"
    source = tmp_path / "floor.blend"
    make = tmp_path / "make-linked.py"
    make.write_text('''import bpy
bpy.ops.wm.read_factory_settings(use_empty=True)
mesh = bpy.data.meshes.new("RequiredMesh")
mesh.from_pydata([(0,0,0),(1,0,0),(0,1,0)],[],[(0,1,2)])
mesh.use_fake_user = True
bpy.ops.wm.save_as_mainfile(filepath=''' + repr(str(dependency)) + ''')
bpy.ops.wm.read_factory_settings(use_empty=True)
with bpy.data.libraries.load(''' + repr(str(dependency)) + ''', link=True) as (source, target):
    target.meshes = ["RequiredMesh"]
obj = bpy.data.objects.new("LinkedPart", target.meshes[0])
bpy.context.scene.collection.objects.link(obj)
bpy.ops.mesh.primitive_cube_add()
bpy.context.object.name = "LocalPart"
bpy.ops.wm.save_as_mainfile(filepath=''' + repr(str(source)) + ''')
''')
    subprocess.run([BLENDER, "-b", "--factory-startup", "--python-exit-code", "1", "--python", str(make)],
                   check=True, capture_output=True, timeout=40)
    data = manifest(source, kind="model")
    plan = tmp_path / "manifest.json"
    plan.write_text(json.dumps(data))
    command = [sys.executable, "-m", "scene_assets", "import", str(plan)]
    # The library is present, but is not pinned in the manifest.
    undeclared = tmp_path / "undeclared.blend"
    result = subprocess.run(command + ["--output", str(undeclared)], capture_output=True, text=True, timeout=40)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "undeclared" in result.stderr.lower()
    assert not undeclared.exists()
    # The source object is local, while its mesh now points to a missing library.
    hidden = tmp_path / "hidden.blend"
    dependency.rename(hidden)
    missing = tmp_path / "missing.blend"
    result = subprocess.run(command + ["--output", str(missing)], capture_output=True, text=True, timeout=40)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "missing" in result.stderr.lower()
    assert not missing.exists()
    # A declared, hash-checked library dependency succeeds.
    hidden.rename(dependency)
    data["files"].append({"path": dependency.name, "source": str(dependency), "size": dependency.stat().st_size,
                          "sha256": assets.digest(dependency)})
    plan.write_text(json.dumps(data))
    declared = tmp_path / "declared.blend"
    result = subprocess.run(command + ["--output", str(declared)], capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stdout + result.stderr
    facts = json.loads(Path(str(declared) + ".assets.json").read_text())[0]
    assert {o["name"] for o in facts["objects"]} == {"LinkedPart", "LocalPart"}
    subprocess.run([BLENDER, "-b", str(declared), "--python-exit-code", "1", "--python-expr",
                    "import bpy; assert not any(i.is_missing for i in bpy.data.user_map()); "
                    "assert len(bpy.context.scene.objects['LinkedPart'].data.vertices) == 3"],
                   check=True, capture_output=True, timeout=40)
