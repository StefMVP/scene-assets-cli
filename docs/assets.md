# Reuse 3D assets

Use library assets before you build common furniture, bedding, materials, or lighting from scratch.
The agent selects the asset and checks the art. The CLI handles files, checksums, and imports.
These commands run locally.

## Select, pin, and import

Install `scene-assets` as shown in the README.

```sh
scene-assets search 'wooden floor' --type textures --limit 5
scene-assets fetch --asset-id wooden_floor_02 --resolution 1k --format blend --dest /path/to/StudioAssets/wooden_floor_02-1k
scene-assets import /path/to/StudioAssets/wooden_floor_02-1k/manifest.json --scene /path/to/project/room.blend --object Floor --output /path/to/project/room-with-floor.blend
```

Use the exact mesh name from your scene for `--object`. The material replaces that mesh's material slots.
Without `--scene`, import creates an empty scene with the source material available by name.
Without `--object`, the import adds the material without assigning it.

For furniture, search with `--type models`. Select an exact catalog ID, such as `side_table_01`.
Fetch with `--format blend`. Import adds the source objects with their numeric transforms intact.
For lighting, search with `--type hdris`. Fetch with `--format exr` or `--format hdr`.
Import sets the scene world to that HDRI at strength 1.
Use an explicit resolution. A selection that the provider does not supply fails.

Use one authoritative scene for all fixed views and camera moves.
Import into that scene, save a new version, and use the same scene for every output.
The importer does not infer room measurements or resize furniture to fit a room.
The receipt records object dimensions, scales, and destination scene units.
Provider dimensions, when present, remain raw source metadata in `source_facts`.
Verify units and placement against the design before use.

## Inspect the source record

Each cache has a `manifest.json`. It records the provider, original URLs, asset ID, authors, license,
resolution, format, use, byte sizes, published MD5 values, and computed SHA-256 values.
Poly Haven models and materials use its `.blend` bundle with every listed dependency.
The CLI preserves the source material nodes and image color spaces. It packs imported images into the saved scene.
The scene's `studio_assets` property and `<output>.assets.json` record the imported sources and actual Blender data.
Keep these records with the project. Link the asset pages in the creative report.
Keep the pinned cache in place when a source uses linked `.blend` libraries.
Each linked library must be present in the manifest. The importer refuses missing Blender data and undeclared libraries.

Choose `--use reused`, `--use adapted`, or `--use reference-only` when fetching from Poly Haven.
Use `adapted` when you intend to change the source. Record later changes in your project report.
A `reference-only` asset cannot be imported as scene content.
No command certifies realism or design approval.

A fetch rerun uses the saved manifest without an API call. It verifies each cached file.
A missing file downloads from its pinned URL. A changed file fails and remains untouched.
A partial fetch keeps its manifest so the same command can resume.
Use a different destination for a different asset, resolution, format, or use.
The CLI does not overwrite an existing scene or remove unrelated files.
A `.asset-lock` file blocks concurrent writes to a cache. After a killed process, check that owner before removing the lock.

## Take in a file from another source

Use `intake` for a trusted local file or a direct HTTPS download from any site.
It takes `.glb`, `.gltf`, `.blend`, `.hdr`, or `.exr`. It writes the manifest from the actual bytes.

```sh
scene-assets intake https://raw.githubusercontent.com/KhronosGroup/glTF-Sample-Assets/main/Models/Duck/glTF/Duck.gltf --dest /path/to/StudioAssets/duck --source-url https://github.com/KhronosGroup/glTF-Sample-Assets/tree/main/Models/Duck
scene-assets import /path/to/StudioAssets/duck/manifest.json --scene /path/to/project/room.blend --output /path/to/project/room-v3.blend
```

- A `.gltf` file's buffers and images are found and pinned at their relative paths.
- Add other dependencies with `--dep SOURCE` or `--dep RELPATH=SOURCE`.
- `--license`, `--author`, `--provider`, `--name`, and `--source-url` record known source facts. A missing license is recorded as `unspecified`.
- Each file is limited to `--max-mb` (512 by default). A partial, corrupt, empty, or oversized intake leaves no files in the cache.
- A GLB must have a valid glTF 2 header and length. A missing dependency fails before the cache is written.
- `source_facts.gltf` records the generator, counts, required extensions, and raw mesh bounds before node transforms.
- glTF units are meters. Blender's importer applies the node transforms and converts Y-up to Z-up. The receipt records the resulting object dimensions.
- Use a new destination for each intake. `fetch --manifest` reruns and verifies the cache offline.

## Compare candidates

`sheet` writes a contact sheet of pinned candidates with their source facts. It reads files and renders no scene.

```sh
scene-assets sheet /path/to/StudioAssets/duck /path/to/StudioAssets/ArmChair_01-1k --output /path/to/project/candidates.png
```

- The picture is the Poly Haven thumbnail, or the first PNG or JPEG embedded in or next to a glTF.
- A glTF texture is labelled "embedded texture, not a model view". A `.blend` from another source shows no picture.
- Each card shows kind, format, use, provider, license, authors, size and its unit, file count, bytes, entry SHA-256, and source URL.
- A `.png` sheet needs `rsvg-convert`. Use a `.svg` output without it.
- For views of the model itself, import it and render the saved scene with Blender.

## Reuse an existing library or another source

Use an explicit version 1 manifest for a local library or a direct HTTPS download.
List every required texture and other dependency at the path used by the `.blend` file.
Archives and automatic archive extraction are not supported.
Use `.blend` for a model or material, and `.hdr` or `.exr` for lighting.
Provide the source license and checksum before a fetch.

```json
{
  "version": 1,
  "provider": "Your licensed library",
  "asset_id": "selected-floor",
  "kind": "material",
  "source_url": "https://example.invalid/assets/selected-floor",
  "license": "Your source license",
  "authors": {"Source artist": "Material"},
  "resolution": "2k",
  "format": "blend",
  "use": "adapted",
  "entrypoint": "floor.blend",
  "files": [
    {
      "path": "floor.blend",
      "source": "/path/to/library/floor.blend",
      "size": 123,
      "sha256": "REPLACE_WITH_64_LOWERCASE_HEX_DIGITS"
    }
  ]
}
```

Use `url` in place of `source` for an HTTPS file. Include all texture entries in `files`.
The example values are placeholders. Obtain sizes and hashes from the actual files or the publisher.

```sh
scene-assets fetch --manifest /path/to/project/source.json --dest /path/to/StudioAssets/selected-floor
scene-assets import /path/to/StudioAssets/selected-floor/manifest.json --output /path/to/project/floor.blend
```

Only import trusted Blender files. Blender runs with automatic script execution disabled.
The CLI rejects absolute cache paths, traversal, duplicate paths, symlinks, missing dependencies, and checksum mismatches.
Blender failures return a nonzero status. The default import timeout is 180 seconds.
Use `--blender /path/to/blender` for a specific installation.

## Provider facts and checks

[Poly Haven's official API guide](https://polyhaven.com/llms.txt) describes the asset catalog, metadata, and download endpoints.
The CLI identifies itself with a User-Agent header and names Poly Haven in discovery output.
[The API terms](https://github.com/Poly-Haven/Public-API/blob/master/ToS.md) apply to API access.
[The asset license](https://polyhaven.com/license) is CC0. Other libraries can have different terms.

