"""Blender entry point for scene-assets import. Run only through the CLI."""
import hashlib
import json
from pathlib import Path
import sys

import bpy

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cli import verify


def run():
    manifest_path, output, object_name = sys.argv[sys.argv.index("--") + 1:]
    manifest_path, output = Path(manifest_path), Path(output)
    manifest = json.loads(manifest_path.read_text())
    verify(manifest, manifest_path.parent)
    receipt = Path(str(output) + ".assets.json")
    if output.exists() or output.is_symlink() or receipt.exists() or receipt.is_symlink():
        raise ValueError("Output or asset receipt already exists")
    if not bpy.data.filepath:
        bpy.ops.object.select_all(action="SELECT")
        bpy.ops.object.delete(use_global=False)
    entry = manifest_path.parent / manifest["entrypoint"]
    before_images = set(bpy.data.images)
    before_data = set(bpy.data.user_map())
    before_libraries = set(bpy.data.libraries)
    materials, objects = [], []
    if manifest["kind"] == "hdri":
        world = bpy.data.worlds.new(manifest["asset_id"])
        world.use_nodes = True
        tree = world.node_tree
        environment = tree.nodes.new("ShaderNodeTexEnvironment")
        environment.image = bpy.data.images.load(str(entry), check_existing=False)
        tree.links.new(environment.outputs["Color"], tree.nodes.get("Background").inputs["Color"])
        bpy.context.scene.world = world
    elif entry.suffix.lower() in (".glb", ".gltf"):
        before_objects = set(bpy.data.objects)
        bpy.ops.import_scene.gltf(filepath=str(entry))
        objects = sorted(set(bpy.data.objects) - before_objects, key=lambda o: o.name)
        if not any(o.type == "MESH" for o in objects):
            raise ValueError("Source file contains no mesh objects")
        materials = list({slot.material for o in objects for slot in o.material_slots if slot.material})
    else:
        with bpy.data.libraries.load(str(entry), link=False) as (source, target):
            if manifest["kind"] == "model":
                target.objects = source.objects
            else:
                target.materials = source.materials
        if manifest["kind"] == "model":
            objects = [o for o in target.objects if o is not None]
            if not objects or not any(o.type == "MESH" for o in objects):
                raise ValueError("Source bundle contains no mesh objects")
            for obj in objects:
                bpy.context.scene.collection.objects.link(obj)
            materials = list({slot.material for o in objects for slot in o.material_slots if slot.material})
        else:
            materials = [m for m in target.materials if m is not None]
            if not materials:
                raise ValueError("Source bundle contains no materials")
            for material in materials:
                material.use_fake_user = True
            if object_name:
                obj = bpy.context.scene.objects.get(object_name)
                if obj is None or obj.type != "MESH":
                    raise ValueError(f"No mesh object named {object_name}")
                if len(materials) != 1:
                    raise ValueError("Assignment needs a bundle with exactly one material")
                obj.data.materials.clear()
                obj.data.materials.append(materials[0])
                objects = [obj]
    allowed = {(manifest_path.parent / f["path"]).resolve() for f in manifest["files"]}
    for block in set(bpy.data.user_map()) - before_data:
        if block.is_missing:
            raise ValueError(f"Missing Blender dependency: {block.name}")
    for library in set(bpy.data.libraries) - before_libraries:
        path = Path(bpy.path.abspath(library.filepath, library=library.parent)).resolve()
        if path not in allowed or not path.is_file():
            raise ValueError(f"Missing or undeclared Blender library dependency: {path}")
    images = []
    for image in bpy.data.images:
        if image in before_images or image.source != "FILE":
            continue
        path = Path(bpy.path.abspath(image.filepath, library=image.library)).resolve()
        if not image.packed_file and (path not in allowed or not path.is_file()):
            raise ValueError(f"Missing or undeclared image dependency: {path}")
        # Appended images are lazy. Access a pixel to ask Blender to decode the file.
        if not image.pixels:
            raise ValueError(f"Image could not be decoded: {path}")
        images.append({"name": image.name, "file": path.name, "size": list(image.size),
                       "colorspace": image.colorspace_settings.name})
        image.pack()
    facts = {"asset_id": manifest["asset_id"], "provider": manifest["provider"],
             "source_url": manifest["source_url"], "license": manifest["license"],
             "authors": manifest.get("authors", {}), "use": manifest["use"],
             "source_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
             "source_files": manifest["files"], "source_facts": manifest.get("source_facts", {}),
             "unit_handling": ("glTF units are meters; Blender scene unit scale unchanged. " if entry.suffix.lower() in (".glb", ".gltf") else "") + "Source object transforms preserved. No unit conversion or room measurements inferred.",
             "scene_units": {"system": bpy.context.scene.unit_settings.system,
                             "scale_length": bpy.context.scene.unit_settings.scale_length},
             "objects": [{"name": o.name, "type": o.type, "scale": list(o.scale),
                          "dimensions": list(o.dimensions)} for o in objects],
             "materials": [{"name": m.name, "nodes": sorted(n.bl_idname for n in m.node_tree.nodes) if m.use_nodes else [],
                            "links": len(m.node_tree.links) if m.use_nodes else 0} for m in materials],
             "images": sorted(images, key=lambda image: image["name"])}
    scene = bpy.context.scene
    history = json.loads(scene.get("studio_assets", "[]"))
    history.append(facts)
    scene["studio_assets"] = json.dumps(history, sort_keys=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(output), check_existing=False)
    with receipt.open("x") as stream:
        stream.write(json.dumps(history, indent=2, sort_keys=True) + "\n")
    print("STUDIO_ASSET_IMPORT " + json.dumps(facts, sort_keys=True))


run()
