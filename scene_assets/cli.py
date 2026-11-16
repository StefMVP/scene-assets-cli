"""Pin library assets and import them with Blender. """
import argparse
from contextlib import contextmanager
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

API = "https://api.polyhaven.com"
USER_AGENT = "scene-assets-cli/0.1"
KINDS = {0: "hdri", 1: "material", 2: "model"}


def api(path):
    with urlopen(Request(API + path, headers={"User-Agent": USER_AGENT}), timeout=30) as response:
        return json.load(response)


def digest(path, algorithm="sha256"):
    value = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError(f"Unsafe asset path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("", ".", "..") for p in value.split("/")):
        raise ValueError(f"Unsafe asset path: {value!r}")
    if value in ("manifest.json", ".asset-lock"):
        raise ValueError(f"Reserved asset path: {value}")
    return path


def inside(root, value):
    path = root / relative(value)
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError(f"Asset path contains a symlink: {value}")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"Asset path leaves the cache: {value}")
    return path


def validate(manifest):
    if not isinstance(manifest, dict):
        raise ValueError("Manifest must be a JSON object")
    if manifest.get("version") != 1 or manifest.get("kind") not in KINDS.values():
        raise ValueError("Expected a version 1 model, material or hdri manifest")
    for field in ("asset_id", "provider", "source_url", "license", "resolution", "format"):
        if not isinstance(manifest.get(field), str) or not manifest[field]:
            raise ValueError(f"Manifest needs {field}")
    if manifest.get("use") not in ("reused", "adapted", "reference-only"):
        raise ValueError("Manifest use must be reused, adapted or reference-only")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("Manifest needs all source files and dependencies")
    paths = set()
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("Each manifest file must be a JSON object")
        path = str(relative(item["path"]))
        if path in paths:
            raise ValueError(f"Duplicate asset path: {path}")
        paths.add(path)
        if type(item.get("size")) is not int or item["size"] <= 0:
            raise ValueError(f"Expected positive size: {path}")
        if not any(item.get(h) for h in ("md5", "sha256")):
            raise ValueError(f"Missing checksum: {path}")
        for algorithm, length in (("md5", 32), ("sha256", 64)):
            if item.get(algorithm) and not re.fullmatch(f"[0-9a-f]{{{length}}}", item[algorithm]):
                raise ValueError(f"Invalid {algorithm}: {path}")
        if bool(item.get("url")) == bool(item.get("source")):
            raise ValueError(f"Use one HTTPS url or local source: {path}")
        if item.get("url"):
            parsed = urlsplit(item["url"])
            if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError(f"Expected a public HTTPS URL: {path}")
        elif not Path(item["source"]).is_absolute():
            raise ValueError(f"Local source must be absolute: {path}")
    entry = str(relative(manifest["entrypoint"]))
    if entry not in paths:
        raise ValueError("Manifest entrypoint is missing from files")
    suffix = PurePosixPath(entry).suffix.lower()
    allowed = {"hdri": (".hdr", ".exr"), "material": (".blend",), "model": (".blend", ".glb", ".gltf")}
    if suffix not in allowed[manifest["kind"]]:
        raise ValueError("Use .blend or .glb/.gltf for models, .blend for materials, .hdr/.exr for HDRIs")
    for path in paths:
        if any(str(parent) in paths for parent in PurePosixPath(path).parents):
            raise ValueError(f"Asset file is also a directory: {path}")
    return manifest


def plan(asset_id, resolution, fmt, use):
    if not re.fullmatch(r"[A-Za-z0-9_]+", asset_id):
        raise ValueError("Select an exact Poly Haven asset ID from search")
    info = api("/info/" + asset_id)
    kind = KINDS[info["type"]]
    fmt = fmt or ("exr" if kind == "hdri" else "blend")
    category = "hdri" if kind == "hdri" else "blend"
    if kind != "hdri" and fmt != "blend":
        raise ValueError("Models and materials use the provider's complete blend bundle")
    try:
        selection = api("/files/" + asset_id)[category][resolution][fmt]
    except KeyError as error:
        raise ValueError(f"No {resolution} {fmt} bundle for {asset_id}") from error
    entry = unquote(urlsplit(selection["url"]).path.rsplit("/", 1)[-1])
    files = []

    def add(path, record):
        files.append({"path": path, **{key: record[key] for key in ("url", "size", "md5")}})
        for dep, metadata in sorted(record.get("include", {}).items()):
            add(dep, metadata)

    add(entry, selection)
    return validate({"version": 1, "provider": "Poly Haven", "asset_id": asset_id,
                     "name": info["name"], "kind": kind, "use": use,
                     "source_url": f"https://polyhaven.com/a/{asset_id}",
                     "license": "CC0-1.0", "license_url": "https://polyhaven.com/license",
                     "authors": info.get("authors", {}), "resolution": resolution, "format": fmt,
                     "source_facts": {k: info[k] for k in ("dimensions", "scale", "files_hash") if k in info},
                     "entrypoint": entry, "files": files})


def verify_file(path, item):
    if not path.is_file() or path.stat().st_size != item["size"]:
        raise ValueError(f"Missing file or size mismatch: {path}")
    for algorithm in ("md5", "sha256"):
        if item.get(algorithm) and digest(path, algorithm) != item[algorithm]:
            raise ValueError(f"{algorithm} mismatch; preserve and inspect this file: {path}")


def verify(manifest, root):
    validate(manifest)
    for item in manifest["files"]:
        verify_file(inside(root, item["path"]), item)
    if PurePosixPath(manifest["entrypoint"]).suffix.lower() in (".glb", ".gltf"):
        gltf_info(inside(root, manifest["entrypoint"]), {f["path"] for f in manifest["files"]})


def gltf_json(path, name=None):
    """Return the JSON of a .gltf or .glb file after a structure check."""
    data, name = path.read_bytes(), name or path.name
    if PurePosixPath(name).suffix.lower() == ".glb":
        if len(data) < 20 or data[:4] != b"glTF":
            raise ValueError(f"Not a GLB file (bad header): {name}")
        version, length = struct.unpack("<II", data[4:12])
        if version != 2 or length != len(data):
            raise ValueError(f"Corrupt GLB (version {version}, header length {length}, file {len(data)}): {name}")
        chunk, kind = struct.unpack("<II", data[12:20])
        if kind != 0x4E4F534A or 20 + chunk > len(data):
            raise ValueError(f"Corrupt GLB JSON chunk: {name}")
        text = data[20:20 + chunk]
    else:
        text = data
    try:
        doc = json.loads(text)
    except ValueError as error:
        raise ValueError(f"Corrupt glTF JSON: {name}") from error
    if not isinstance(doc, dict) or not str(doc.get("asset", {}).get("version", "")).startswith("2"):
        raise ValueError(f"Expected glTF 2.x: {name}")
    return doc


def gltf_uris(doc):
    return [e["uri"] for key in ("buffers", "images") for e in doc.get(key, [])
            if isinstance(e, dict) and isinstance(e.get("uri"), str) and not e["uri"].startswith("data:")]


def gltf_info(path, declared=None):
    """Check external dependencies against declared paths and return raw source facts."""
    doc = gltf_json(path)
    for uri in gltf_uris(doc):
        if declared is not None and str(relative(unquote(uri))) not in declared:
            raise ValueError(f"Missing glTF dependency in manifest: {uri}")
    low, high = None, None
    for mesh in doc.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            accessor = doc.get("accessors", [])
            index = primitive.get("attributes", {}).get("POSITION")
            if index is None or index >= len(accessor):
                continue
            lo, hi = accessor[index].get("min"), accessor[index].get("max")
            if lo and hi and len(lo) == 3 and len(hi) == 3:
                low = lo if low is None else [min(a, b) for a, b in zip(low, lo)]
                high = hi if high is None else [max(a, b) for a, b in zip(high, hi)]
    facts = {"generator": doc["asset"].get("generator"), "meshes": len(doc.get("meshes", [])),
             "materials": len(doc.get("materials", [])), "images": len(doc.get("images", [])),
             "extensions_required": doc.get("extensionsRequired", []), "units": "meters (glTF)"}
    if low:
        facts["mesh_bounds"] = {"min": low, "max": high, "size": [round(b - a, 6) for a, b in zip(low, high)],
                                "note": "Raw mesh positions before node transforms."}
    return facts


@contextmanager
def lock(root):
    root.mkdir(parents=True, exist_ok=True)
    lockfile = root / ".asset-lock"
    try:
        fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as error:
        raise ValueError(f"Asset cache is locked: {lockfile}. Check the owner before removing the lock.") from error
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(str(os.getpid()) + "\n")
        yield
    finally:
        lockfile.unlink()


def write_json(path, data):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".asset-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def download(root, item):
    target = inside(root, item["path"])
    if target.exists():
        verify_file(target, item)
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=target.parent, prefix=".asset-")
    try:
        with os.fdopen(fd, "wb") as output:
            if item.get("url"):
                source = urlopen(Request(item["url"], headers={"User-Agent": USER_AGENT}), timeout=60)
            else:
                source = Path(item["source"]).open("rb")
            with source:
                remaining = item["size"]
                while True:
                    block = source.read(min(1024 * 1024, remaining + 1))
                    if not block:
                        break
                    remaining -= len(block)
                    if remaining < 0:
                        raise ValueError(f"Download exceeds pinned size: {item['path']}")
                    output.write(block)
        verify_file(Path(temporary), item)
        # Exclusive publication preserves files created by other programs during the download.
        os.link(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def open_source(source):
    """Open a trusted local file or a public HTTPS URL for reading."""
    if source.startswith("https://"):
        parsed = urlsplit(source)
        if not parsed.hostname or parsed.username or parsed.password:
            raise ValueError(f"Expected a public HTTPS URL: {source}")
        response = urlopen(Request(source, headers={"User-Agent": USER_AGENT}), timeout=60)
        if not response.geturl().startswith("https://"):
            response.close()
            raise ValueError(f"Download redirected away from HTTPS: {source}")
        return response
    if "://" in source:
        raise ValueError(f"Use a local absolute path or an https:// URL: {source}")
    path = Path(source).expanduser()
    if not path.is_absolute() or not path.is_file():
        raise ValueError(f"Local source must be an absolute path to a file: {source}")
    return path.resolve().open("rb")


def intake_copy(root, source, rel, limit):
    """Copy one source into a temporary file inside the cache. Hash the actual bytes."""
    fd, temporary = tempfile.mkstemp(dir=root, prefix=".asset-")
    value, size = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as output, open_source(source) as stream:
            while True:
                block = stream.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > limit:
                    raise ValueError(f"{rel} exceeds the {limit // 1048576} MB intake limit")
                value.update(block)
                output.write(block)
        if size == 0:
            raise ValueError(f"Source is empty: {source}")
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return Path(temporary), size, value.hexdigest()


def sibling(source, uri):
    """Locate a glTF dependency next to its entry file."""
    if source.startswith("https://"):
        return urljoin(source, quote(unquote(uri), safe="/"))
    return str(Path(source).expanduser().parent / unquote(uri))


def intake(args):
    """Pin a trusted local or direct HTTPS GLB, glTF, Blender or HDRI file without a hand-written manifest."""
    if "://" in args.source and not args.source.startswith("https://"):
        raise ValueError(f"Use a local absolute path or an https:// URL: {args.source}")
    source = args.source if args.source.startswith("https://") else str(Path(args.source).expanduser().absolute())
    name = unquote(urlsplit(source).path.rsplit("/", 1)[-1]) if source.startswith("https://") else Path(source).name
    suffix = PurePosixPath(name).suffix.lower()
    kind = args.kind or ("hdri" if suffix in (".hdr", ".exr") else "model")
    if suffix not in (".glb", ".gltf", ".blend", ".hdr", ".exr"):
        raise ValueError("Intake takes .glb, .gltf, .blend, .hdr or .exr")
    relative(name)
    root = args.dest.expanduser().resolve()
    limit = args.max_mb * 1048576
    asset_id = args.asset_id or re.sub(r"[^A-Za-z0-9_]+", "_", PurePosixPath(name).stem).strip("_") or "asset"
    host = urlsplit(source).hostname if source.startswith("https://") else None
    with lock(root):
        if (root / "manifest.json").exists() or (root / "manifest.json").is_symlink():
            raise ValueError("Destination already holds a manifest; choose a new destination")
        staged = {}  # path -> (temporary, size, sha256, origin)
        try:
            staged[name] = (*intake_copy(root, source, name, limit), source)
            facts = {}
            if suffix in (".glb", ".gltf"):
                if kind != "model":
                    raise ValueError("GLB and glTF files are models")
                doc = gltf_json(staged[name][0], name)
                for uri in gltf_uris(doc):
                    rel = str(relative(unquote(uri)))
                    if rel not in staged:
                        origin = sibling(source, uri)
                        try:
                            staged[rel] = (*intake_copy(root, origin, rel, limit), origin)
                        except (OSError, ValueError) as error:
                            raise ValueError(f"glTF dependency {uri} could not be packaged: {error}") from error
            for spec in args.dep or []:
                left, _, right = spec.partition("=")
                rel, origin = (left, right) if right and ":" not in left else (PurePosixPath(urlsplit(spec).path).name, spec)
                rel = str(relative(unquote(rel)))
                if rel not in staged:
                    staged[rel] = (*intake_copy(root, origin, rel, limit), origin)
            files = []
            for rel, (temporary, size, sha, origin) in sorted(staged.items()):
                files.append({"path": rel, "size": size, "sha256": sha,
                              ("url" if origin.startswith("https://") else "source"): origin})
            manifest = {"version": 1, "provider": args.provider or host or "Local file", "asset_id": asset_id,
                        "name": args.name or asset_id, "kind": kind, "use": args.use,
                        "source_url": args.source_url or (source if host else Path(source).as_uri()),
                        "license": args.license or "unspecified", "authors": dict.fromkeys(args.author or [], "Source"),
                        "resolution": "source", "format": suffix.lstrip("."), "entrypoint": name, "files": files}
            validate(manifest)
            for rel, (temporary, *_rest) in staged.items():
                inside(root, rel)  # refuses symlinks and escapes
                if (root / rel).exists():
                    raise ValueError(f"Destination already holds {rel}; choose a new destination")
            for rel, (temporary, *_rest) in staged.items():
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                os.link(temporary, target)
            if suffix in (".glb", ".gltf"):
                manifest["source_facts"] = {"gltf": gltf_info(root / name, set(staged))}
            write_json(root / "manifest.json", manifest)
            verify(manifest, root)
        finally:
            for temporary, *_rest in staged.values():
                temporary.unlink(missing_ok=True)
    return {"manifest": str(root / "manifest.json"), "provider": manifest["provider"], "asset_id": asset_id,
            "kind": kind, "files": len(files), "bytes": sum(f["size"] for f in files),
            "source_facts": manifest.get("source_facts", {})}


def fetch(args):
    root = args.dest.expanduser().resolve()
    with lock(root):
        saved = root / "manifest.json"
        if saved.is_symlink():
            raise ValueError("The cache manifest must not be a symlink")
        if saved.exists():
            manifest = validate(json.loads(saved.read_text()))
            if args.manifest:
                requested = validate(json.loads(args.manifest.read_text()))
                # A direct manifest may omit the SHA-256 values added after its first fetch.
                for item in requested["files"]:
                    matching = next((i for i in manifest["files"] if i["path"] == item["path"]), {})
                    if any(matching.get(k) != v for k, v in item.items()):
                        raise ValueError("Destination is pinned to another manifest")
                if any(manifest.get(k) != v for k, v in requested.items() if k != "files") or len(requested["files"]) != len(manifest["files"]):
                    raise ValueError("Destination is pinned to another manifest")
            elif (args.asset_id != manifest["asset_id"] or args.resolution != manifest["resolution"]
                  or (args.format and args.format != manifest["format"]) or args.use != manifest["use"]):
                raise ValueError("Destination is pinned to another asset or selection")
        else:
            manifest = (validate(json.loads(args.manifest.read_text())) if args.manifest
                        else plan(args.asset_id, args.resolution, args.format, args.use))
            write_json(saved, manifest)
        for item in manifest["files"]:
            download(root, item)
            item["sha256"] = digest(inside(root, item["path"]))
        verify(manifest, root)
        write_json(saved, manifest)
    return {"manifest": str(saved), "provider": manifest["provider"], "asset_id": manifest["asset_id"],
            "files": len(manifest["files"]), "bytes": sum(i["size"] for i in manifest["files"])}


def import_asset(args):
    manifest_path = args.manifest.expanduser().resolve()
    manifest = json.loads(manifest_path.read_text())
    verify(manifest, manifest_path.parent)
    if manifest["use"] == "reference-only":
        raise ValueError("A reference-only asset cannot be imported as scene content")
    output = args.output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError(f"Output exists; choose a new scene path: {output}")
    if args.object and (not args.scene or manifest["kind"] != "material"):
        raise ValueError("--object needs --scene and a material asset")
    if args.scene and not args.scene.is_file():
        raise ValueError(f"Scene does not exist: {args.scene}")
    output.parent.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).with_name("blender_import.py")
    command = [args.blender, "--background", "--factory-startup", "--disable-autoexec"]
    if args.scene:
        command += [str(args.scene.resolve())]
    command += ["--python-exit-code", "1", "--python", str(script), "--", str(manifest_path), str(output), args.object or ""]
    subprocess.run(command, check=True, timeout=args.timeout)
    if not output.is_file():
        raise ValueError("Blender returned without the scene file")
    return {"scene": str(output), "facts": str(output) + ".assets.json"}


def esc(text):
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def preview_of(root, manifest):
    """Return (label, mime, bytes) for a cheap existing preview image, or None. Nothing is rendered."""
    if manifest["provider"] == "Poly Haven":
        try:
            url = f"https://cdn.polyhaven.com/asset_img/thumbs/{manifest['asset_id']}.png?height=256"
            with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=15) as response:
                return "provider thumbnail", "image/png", response.read(4 * 1048576)
        except OSError:
            pass
    entry = inside(root, manifest["entrypoint"])
    if entry.suffix.lower() in (".glb", ".gltf"):
        doc = gltf_json(entry)
        blob = entry.read_bytes()
        views = doc.get("bufferViews", [])
        for image in doc.get("images", []):
            mime = image.get("mimeType")
            data = None
            if "bufferView" in image and entry.suffix.lower() == ".glb":
                chunk = struct.unpack("<I", blob[12:16])[0]
                start = 20 + chunk + 8  # BIN chunk data
                view = views[image["bufferView"]]
                data = blob[start + view.get("byteOffset", 0):start + view.get("byteOffset", 0) + view["byteLength"]]
            elif isinstance(image.get("uri"), str) and not image["uri"].startswith("data:"):
                path = inside(root, str(relative(unquote(image["uri"]))))
                mime, data = mime or ("image/png" if path.suffix.lower() == ".png" else "image/jpeg"), path.read_bytes()
            if data and mime in ("image/png", "image/jpeg") and len(data) <= 8 * 1048576:
                return "embedded texture, not a model view", mime, data
    return None


def sheet(args):
    """Write a candidate contact sheet from pinned manifests. It reads files and renders no scene."""
    cards = []
    for given in args.candidates:
        path = given.expanduser().resolve()
        path = path / "manifest.json" if path.is_dir() else path
        manifest = json.loads(path.read_text())
        verify(manifest, path.parent)
        facts = manifest.get("source_facts", {})
        bounds = facts.get("gltf", {}).get("mesh_bounds", {}).get("size")
        size, unit = (bounds, "m raw mesh") if bounds else (facts.get("dimensions"), "provider's raw units")
        cards.append({"manifest": str(path), "asset_id": manifest["asset_id"], "name": manifest.get("name", ""),
                      "kind": manifest["kind"], "format": manifest["format"], "provider": manifest["provider"],
                      "source_url": manifest["source_url"], "license": manifest["license"],
                      "authors": ", ".join(manifest.get("authors", {})) or "-", "use": manifest["use"],
                      "files": len(manifest["files"]), "bytes": sum(f["size"] for f in manifest["files"]),
                      "size": size, "size_unit": unit, "sha256": next(f for f in manifest["files"] if f["path"] == manifest["entrypoint"]).get("sha256", "")[:12],
                      "preview": preview_of(path.parent, manifest)})
    width, height, columns = 360, 400, min(len(cards), 4)
    rows = -(-len(cards) // columns)
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{columns * width}" height="{rows * height}" font-family="DejaVu Sans, sans-serif" font-size="12">',
             '<rect width="100%" height="100%" fill="#f4f4f2"/>']
    for n, card in enumerate(cards):
        x, y = n % columns * width, n // columns * height
        parts.append(f'<g transform="translate({x},{y})"><rect x="6" y="6" width="{width - 12}" height="{height - 12}" fill="#fff" stroke="#bbb"/>')
        parts.append(f'<text x="16" y="28" font-size="15" font-weight="bold">{n + 1}. {esc(card["asset_id"])[:38]}</text>')
        if card["preview"]:
            label, mime, data = card["preview"]
            parts.append(f'<image x="16" y="38" width="328" height="200" preserveAspectRatio="xMidYMid meet" href="data:{mime};base64,{base64.b64encode(data).decode()}"/>')
            parts.append(f'<text x="16" y="252" fill="#666" font-size="10">{esc(label)}</text>')
        else:
            parts.append('<rect x="16" y="38" width="328" height="200" fill="#eee"/><text x="180" y="142" text-anchor="middle" fill="#888">no preview image in source</text>')
        dims = " x ".join(f"{v:g}" for v in card["size"]) + " " + card["size_unit"] if isinstance(card["size"], list) and len(card["size"]) == 3 else "unknown"
        lines = [f'{card["kind"]} / {card["format"]}  use: {card["use"]}', f'name: {card["name"]}', f'provider: {card["provider"]}',
                 f'license: {card["license"]}', f'authors: {card["authors"]}', f'size: {dims}',
                 f'{card["files"]} file(s), {card["bytes"]} bytes, sha256 {card["sha256"]}', f'source: {card["source_url"]}']
        for i, line in enumerate(lines):
            parts.append(f'<text x="16" y="{274 + i * 16}">{esc(line if len(line) <= 50 else line[:49] + "...")}</text>')
        parts.append("</g>")
    parts.append("</svg>")
    output = args.output.expanduser().absolute()
    if output.exists() or output.is_symlink():
        raise ValueError(f"Output exists; choose a new path: {output}")
    if output.suffix.lower() not in (".png", ".svg"):
        raise ValueError("Sheet output must end in .png or .svg")
    output.parent.mkdir(parents=True, exist_ok=True)
    svg = "\n".join(parts)
    if output.suffix.lower() == ".svg":
        output.write_text(svg)
    else:
        tool = shutil.which("rsvg-convert")
        if not tool:
            raise ValueError("A .png sheet needs rsvg-convert on PATH; use a .svg output otherwise")
        subprocess.run([tool, "-o", str(output)], input=svg.encode(), check=True, timeout=60)
    return {"sheet": str(output), "candidates": [{k: v for k, v in c.items() if k != "preview"} | {"preview": c["preview"][0] if c["preview"] else None} for c in cards]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="List actual Poly Haven catalog IDs and metadata")
    search.add_argument("query", nargs="?", default="")
    search.add_argument("--type", choices=("hdris", "textures", "models"), default="models")
    search.add_argument("--limit", type=int, choices=range(1, 51), default=10, metavar="1..50")
    pin = commands.add_parser("fetch", help="Pin a selected bundle or fetch an explicit manifest")
    selection = pin.add_mutually_exclusive_group(required=True)
    selection.add_argument("--asset-id")
    selection.add_argument("--manifest", type=Path)
    pin.add_argument("--resolution", default="1k")
    pin.add_argument("--format", choices=("blend", "exr", "hdr"))
    pin.add_argument("--use", choices=("reused", "adapted", "reference-only"), default="reused")
    pin.add_argument("--dest", type=Path, required=True)
    take = commands.add_parser("intake", help="Pin a trusted local or direct HTTPS .glb/.gltf/.blend/.hdr/.exr file")
    take.add_argument("source", help="Absolute local path or https:// URL")
    take.add_argument("--dest", type=Path, required=True)
    take.add_argument("--kind", choices=("model", "material", "hdri"))
    take.add_argument("--asset-id")
    take.add_argument("--name")
    take.add_argument("--provider")
    take.add_argument("--source-url", help="Page that describes the asset; default is the file source")
    take.add_argument("--license", help="Recorded as given when known; default 'unspecified'")
    take.add_argument("--author", action="append")
    take.add_argument("--use", choices=("reused", "adapted", "reference-only"), default="reused")
    take.add_argument("--dep", action="append", help="Extra dependency, SOURCE or RELPATH=SOURCE (.gltf dependencies are found automatically)")
    take.add_argument("--max-mb", type=int, default=512, metavar="MB")
    cards = commands.add_parser("sheet", help="Make a contact sheet of pinned candidates with source metadata")
    cards.add_argument("candidates", nargs="+", type=Path, help="Cache folders or manifest.json files")
    cards.add_argument("--output", type=Path, required=True)
    imp = commands.add_parser("import", help="Append the pinned asset and save a packed Blender scene")
    imp.add_argument("manifest", type=Path)
    imp.add_argument("--output", type=Path, required=True)
    imp.add_argument("--scene", type=Path)
    imp.add_argument("--object", help="Assign the imported material to this scene object")
    imp.add_argument("--blender", default="blender")
    imp.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args(argv)
    try:
        if args.command == "search":
            catalog = api("/assets?" + urlencode({"type": args.type}))
            words = args.query.lower().split()
            result = []
            for slug, info in sorted(catalog.items()):
                text = " ".join([slug, info["name"], *info.get("tags", []), *info.get("categories", [])]).lower()
                if all(word in text for word in words):
                    result.append({"asset_id": slug, "name": info["name"], "authors": info.get("authors", {}),
                                   "categories": info.get("categories", []), "source_url": f"https://polyhaven.com/a/{slug}"})
            data = {"provider": "Poly Haven", "type": args.type, "matches": len(result), "assets": result[:args.limit]}
        elif args.command == "fetch":
            data = fetch(args)
        elif args.command == "intake":
            data = intake(args)
        elif args.command == "sheet":
            data = sheet(args)
        else:
            data = import_asset(args)
        print(json.dumps(data, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError, struct.error) as error:
        print(f"assets: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
