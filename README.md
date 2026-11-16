# Scene Assets CLI

Find library assets, pin their files and dependencies, compare candidates, and
import them into a saved Blender scene. Models, materials, and HDRIs keep their
source facts and checksums with the project.

## Install

Use Python 3.11 or newer. Install Blender on PATH for imports.
Search, fetch, and intake use the Python standard library.
PNG sheets also need `rsvg-convert`. SVG sheets need no extra tool.

```sh
git clone https://github.com/StefMVP/scene-assets-cli.git
cd scene-assets-cli
python3 -m pip install .
scene-assets --help
```

## Select and import an asset

```sh
scene-assets search 'wooden floor' --type textures --limit 5
scene-assets fetch --asset-id wooden_floor_02 --resolution 1k --format blend --dest assets/floor-1k
scene-assets import assets/floor-1k/manifest.json --output scenes/floor.blend
```

Search returns actual Poly Haven IDs and source pages. Fetch writes the complete
bundle and `manifest.json`. Import saves the scene and `floor.blend.assets.json`.
With no input scene, the material is available by name in an empty scene.
Use `--scene room.blend --object Floor` to assign it to an existing mesh.

Use `--type models` to find furniture. Use `--type hdris` and fetch with
`--format exr` to set a scene's world lighting. Choose one saved scene for all
fixed views and camera moves. Check asset units and placement before use.

```sh
scene-assets intake /absolute/path/to/chair.glb --dest assets/chair --license CC0-1.0
scene-assets sheet assets/chair assets/floor-1k --output candidates.svg
```

Intake supports trusted local files and direct HTTPS downloads of `.glb`, `.gltf`,
`.blend`, `.hdr`, and `.exr` files. It records an unknown license as `unspecified`.
Supply known license and author facts. Keep downloaded assets out of this repository.

Fetch reruns verify the pinned cache without an API call. Changed files fail.
Use a new destination for each selection. Import and sheet refuse existing outputs.
Only import trusted Blender files. Automatic script execution is disabled.
The importer preserves source transforms and packs imported images. Keep the cache
for assets with linked Blender libraries.

See [asset commands](docs/assets.md) for dependencies, source manifests, and receipts.
