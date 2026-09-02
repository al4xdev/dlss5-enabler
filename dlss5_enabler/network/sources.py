import json
import re
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, cast

from dlss5_enabler.core.archive import safe_archive_destination
from dlss5_enabler.core.fileio import atomic_write_bytes, atomic_write_text, resource_lock
from dlss5_enabler.core.record import BinaryInfo
from dlss5_enabler.core.util import get_cache_dir, sha256_file
from dlss5_enabler.network.http import http_download_file, http_get_json, http_get_text

LogFn = Callable[[str], None]
ProgressFn = Callable[[int, int], None]


def _version_key(value: str) -> tuple[int, ...]:
    numbers = tuple(int(part) for part in re.findall(r"\d+", value))
    return numbers or (0,)


def _cached_download(
    dest: Path,
    url: str,
    revision: str,
    progress: ProgressFn | None,
    force: bool,
) -> Path:
    metadata_path = Path(f"{dest}.dlss5-enabler-cache.json")
    state_lock = Path(f"{dest}.cache-state")
    with resource_lock(state_lock):
        valid = False
        if not force and dest.is_file() and dest.stat().st_size > 0 and metadata_path.is_file():
            try:
                raw: Any = json.loads(metadata_path.read_text(encoding="utf-8"))
                metadata = cast(dict[str, Any], raw)
                valid = (
                    metadata.get("url") == url
                    and metadata.get("revision") == revision
                    and metadata.get("sha256") == sha256_file(dest)
                )
            except Exception:
                valid = False
        if not valid:
            http_download_file(url, dest, progress_fn=progress)
            metadata = {"url": url, "revision": revision, "sha256": sha256_file(dest)}
            atomic_write_text(metadata_path, json.dumps(metadata, sort_keys=True))
    return dest


def _branch_revision(repository: str, branch: str) -> str:
    try:
        raw: Any = http_get_json(f"https://api.github.com/repos/{repository}/commits/{branch}")
        data = cast(dict[str, Any], raw)
        sha = str(data.get("sha", ""))
        return sha or branch
    except Exception:
        return branch


def zip_extract_matching(
    zip_path: Path | str,
    dest_dir: Path | str,
    patterns: list[str],
    flatten: bool = True,
) -> list[Path]:
    zpath: Path = Path(zip_path)
    dest: Path = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    extracted: list[Path] = []

    with zipfile.ZipFile(zpath, "r") as zf:
        destinations: set[Path] = set()
        for info in zf.infolist():
            if info.is_dir():
                continue
            name: str = info.filename.replace("\\", "/")
            matched: bool = any(fnmatch(name.lower(), p.lower()) for p in patterns)
            if not matched:
                continue

            out_path = safe_archive_destination(dest, name, flatten=flatten)
            if out_path in destinations:
                raise ValueError(f"Archive members collide at destination: {out_path.name}")
            destinations.add(out_path)
            with resource_lock(out_path):
                atomic_write_bytes(out_path, zf.read(info))
            extracted.append(out_path)

    if not extracted:
        raise ValueError(f"No matching files in {zpath.name} for patterns: {patterns}")
    return extracted


class FeederBundle:
    def __init__(self) -> None:
        self.release_tag: str = ""
        self.addon64: Path | None = None
        self.addon32: Path | None = None
        self.fx_shader: Path | None = None
        self.host64_exe: Path | None = None
        self.vk_layer_zip: Path | None = None
        self.binaries: dict[str, BinaryInfo] = {}


def fetch_feeder(log: LogFn, progress: ProgressFn | None = None, force: bool = False) -> FeederBundle:
    out: FeederBundle = FeederBundle()
    base_url: str = "https://github.com/jlrouzies-fr/DLSS5-Feeder/releases/latest/download"
    cache_dir: Path = get_cache_dir()

    try:
        raw_json: Any = http_get_json("https://api.github.com/repos/jlrouzies-fr/DLSS5-Feeder/releases/latest")
        j: dict[str, Any] = cast(dict[str, Any], raw_json)
        out.release_tag = str(j.get("tag_name", "latest"))
    except Exception:
        out.release_tag = "latest"

    log(f"DLSS5-Feeder release: {out.release_tag}")

    files_to_dl: list[tuple[str, str]] = [
        ("dlss5-feed.addon64", base_url + "/dlss5-feed.addon64"),
        ("dlss5-feed.addon32", base_url + "/dlss5-feed.addon32"),
        ("DLSS5_Feed.fx", base_url + "/DLSS5_Feed.fx"),
        ("dlss5-feed-host64.exe", base_url + "/dlss5-feed-host64.exe"),
    ]

    for name, url in files_to_dl:
        dest: Path = cache_dir / name
        if force or not dest.exists():
            log(f"  Downloading {name}...")
        _cached_download(dest, url, out.release_tag, progress, force or out.release_tag == "latest")
        setattr(out, name.replace(".", "_").replace("-", "_"), dest)
        out.binaries[name] = BinaryInfo(
            name=name,
            version=out.release_tag,
            sha256=sha256_file(dest),
            size_bytes=dest.stat().st_size,
            source_url=url,
        )

    out.addon64 = cache_dir / "dlss5-feed.addon64"
    out.addon32 = cache_dir / "dlss5-feed.addon32"
    out.fx_shader = cache_dir / "DLSS5_Feed.fx"
    out.host64_exe = cache_dir / "dlss5-feed-host64.exe"

    vk_dest: Path = cache_dir / "feed-vk-layer.zip"
    vk_url: str = base_url + "/feed-vk-layer.zip"
    try:
        _cached_download(vk_dest, vk_url, out.release_tag, progress, force or out.release_tag == "latest")
        if vk_dest.exists():
            out.vk_layer_zip = vk_dest
            out.binaries["feed-vk-layer.zip"] = BinaryInfo(
                name="feed-vk-layer.zip",
                version=out.release_tag,
                sha256=sha256_file(vk_dest),
                size_bytes=vk_dest.stat().st_size,
                source_url=vk_url,
            )
    except Exception:
        log("  feed-vk-layer.zip not published in this release (Vulkan layer unavailable).")

    return out


class RenoDxBundle:
    def __init__(self) -> None:
        self.version: str = ""
        self.addon64_path: Path | None = None
        self.binaries: dict[str, BinaryInfo] = {}


def fetch_renodx_dlss5(log: LogFn, progress: ProgressFn | None = None, force: bool = False) -> RenoDxBundle:
    out: RenoDxBundle = RenoDxBundle()
    log("Querying RankFTW/rhi-repo for the newest renodx-dlss5 release...")
    cache_dir: Path = get_cache_dir()

    raw_data: Any = http_get_json("https://api.github.com/repos/RankFTW/rhi-repo/releases?per_page=100")
    data: list[dict[str, Any]] = cast(list[dict[str, Any]], raw_data)
    rels: list[dict[str, str]] = []
    for rel in data:
        tag: str = str(rel.get("tag_name", ""))
        if not tag.lower().startswith("renodx-dlss5-"):
            continue
        assets: list[dict[str, Any]] = cast(list[dict[str, Any]], rel.get("assets", []))
        for a in assets:
            name: str = str(a.get("name", ""))
            if name.lower().endswith(".zip"):
                rels.append(
                    {
                        "tag": tag,
                        "version": tag[len("renodx-dlss5-") :],
                        "url": str(a.get("browser_download_url", "")),
                        "name": name,
                    }
                )
                break

    if not rels:
        raise RuntimeError("No renodx-dlss5 releases found in RankFTW/rhi-repo")

    newest: dict[str, str] = max(rels, key=lambda item: _version_key(item["version"]))
    out.version = newest["version"]
    log(f"renodx-dlss5 newest release: {out.version}")

    zip_dest: Path = cache_dir / "renodx-dlss5.zip"
    if force or not zip_dest.exists():
        log(f"  Downloading {newest['name']}...")
    _cached_download(zip_dest, newest["url"], newest["tag"], progress, force)

    extracted = zip_extract_matching(zip_dest, cache_dir, ["*renodx-dlss5.addon64"], flatten=True)
    out.addon64_path = extracted[0]

    out.binaries["renodx-dlss5.addon64"] = BinaryInfo(
        name="renodx-dlss5.addon64",
        version=out.version,
        sha256=sha256_file(out.addon64_path),
        size_bytes=out.addon64_path.stat().st_size,
        source_url=newest["url"],
    )
    return out


class NgxBundle:
    def __init__(self) -> None:
        self.nr_version: str = ""
        self.nr_dll_path: Path | None = None
        self.sr_version: str = ""
        self.sr_dll_path: Path | None = None
        self.binaries: dict[str, BinaryInfo] = {}


def fetch_ngx_dlls(log: LogFn, progress: ProgressFn | None = None, force: bool = False) -> NgxBundle:
    out: NgxBundle = NgxBundle()
    log("Fetching RHI dlss_manifest.json (DLSS NR / SR sources)...")
    cache_dir: Path = get_cache_dir()

    raw_manifest: Any = http_get_json("https://raw.githubusercontent.com/RankFTW/RHI/main/dlss_manifest.json")
    manifest: dict[str, Any] = cast(dict[str, Any], raw_manifest)

    dlssnr_list: list[dict[str, Any]] = cast(list[dict[str, Any]], manifest.get("dlssnr", []))
    if not dlssnr_list:
        raise RuntimeError("No dlssnr entries in dlss_manifest.json")

    sf_builds: list[dict[str, Any]] = [e for e in dlssnr_list if "SF" in str(e.get("version", ""))]
    nr_candidates = sf_builds if sf_builds else dlssnr_list
    best_nr: dict[str, Any] = max(nr_candidates, key=lambda item: _version_key(str(item.get("version", ""))))
    out.nr_version = str(best_nr.get("version", ""))
    log(f"nvngx_dlssnr newest version: {out.nr_version}")

    nr_zip: Path = cache_dir / "nvngx_dlssnr.zip"
    nr_url = str(best_nr["url"])
    _cached_download(nr_zip, nr_url, out.nr_version, progress, force)
    out.nr_dll_path = zip_extract_matching(nr_zip, cache_dir, ["*nvngx_dlssnr.dll"], flatten=True)[0]
    out.binaries["nvngx_dlssnr.dll"] = BinaryInfo(
        name="nvngx_dlssnr.dll",
        version=out.nr_version,
        sha256=sha256_file(out.nr_dll_path),
        size_bytes=out.nr_dll_path.stat().st_size,
        source_url=nr_url,
    )

    dlss_list: list[dict[str, Any]] = cast(list[dict[str, Any]], manifest.get("dlss", []))
    if not dlss_list:
        raise RuntimeError("No dlss entries in dlss_manifest.json")

    best_sr: dict[str, Any] = max(dlss_list, key=lambda item: _version_key(str(item.get("version", ""))))
    out.sr_version = str(best_sr.get("version", ""))
    log(f"nvngx_dlss newest version: {out.sr_version}")

    sr_zip: Path = cache_dir / "nvngx_dlss.zip"
    sr_url = str(best_sr["url"])
    _cached_download(sr_zip, sr_url, out.sr_version, progress, force)
    out.sr_dll_path = zip_extract_matching(sr_zip, cache_dir, ["*nvngx_dlss.dll"], flatten=True)[0]
    out.binaries["nvngx_dlss.dll"] = BinaryInfo(
        name="nvngx_dlss.dll",
        version=out.sr_version,
        sha256=sha256_file(out.sr_dll_path),
        size_bytes=out.sr_dll_path.stat().st_size,
        source_url=sr_url,
    )

    return out


class ReshadeBundle:
    def __init__(self) -> None:
        self.version: str = ""
        self.setup_exe_path: Path | None = None
        self.binaries: dict[str, BinaryInfo] = {}


def fetch_reshade(log: LogFn, progress: ProgressFn | None = None, force: bool = False) -> ReshadeBundle:
    out: ReshadeBundle = ReshadeBundle()
    log("Checking reshade.me for the current version...")
    cache_dir: Path = get_cache_dir()

    page: str = http_get_text("https://reshade.me/")
    matches: list[str] = re.findall(r"reshade_setup_([0-9\.]+)_addon\.exe", page, re.IGNORECASE)
    if not matches:
        raise RuntimeError("Could not scrape ReShade Addon version from reshade.me")

    out.version = max(matches, key=_version_key)
    log(f"ReShade latest version: {out.version}")

    url: str = f"https://reshade.me/downloads/ReShade_Setup_{out.version}_Addon.exe"
    exe_path: Path = cache_dir / f"ReShade_Setup_{out.version}_Addon.exe"
    if force or not exe_path.exists():
        log(f"  Downloading ReShade Setup {out.version}...")
    _cached_download(exe_path, url, out.version, progress, force)

    out.setup_exe_path = exe_path
    out.binaries["ReShade_Setup"] = BinaryInfo(
        name=f"ReShade_Setup_{out.version}_Addon.exe",
        version=out.version,
        sha256=sha256_file(exe_path),
        size_bytes=exe_path.stat().st_size,
        source_url=url,
    )
    return out


class LumeniteBundle:
    def __init__(self) -> None:
        self.branch: str = "main"
        self.staging_dir: Path | None = None
        self.files: list[Path] = []
        self.binaries: dict[str, BinaryInfo] = {}


def fetch_lumenite(log: LogFn, progress: ProgressFn | None = None, force: bool = False) -> LumeniteBundle:
    out: LumeniteBundle = LumeniteBundle()
    log("Fetching LumeniteFX (motion-vector provider) from github.com/umar-afzaal/LumeniteFX...")
    cache_dir: Path = get_cache_dir()

    try:
        raw_meta: Any = http_get_json("https://api.github.com/repos/umar-afzaal/LumeniteFX")
        meta: dict[str, Any] = cast(dict[str, Any], raw_meta)
        out.branch = str(meta.get("default_branch", "main"))
    except Exception:
        out.branch = "main"

    log(f"  source branch: {out.branch}")
    revision = _branch_revision("umar-afzaal/LumeniteFX", out.branch)

    zip_path: Path = cache_dir / "LumeniteFX.zip"
    url: str = f"https://codeload.github.com/umar-afzaal/LumeniteFX/zip/refs/heads/{out.branch}"
    _cached_download(zip_path, url, revision, progress, force or revision == out.branch)

    safe_revision = re.sub(r"[^A-Za-z0-9_.-]", "_", revision)[:24]
    staging: Path = cache_dir / f"lumenite_stage_{safe_revision}"
    marker = staging / ".complete"
    with resource_lock(staging):
        if force or revision == out.branch or not marker.is_file():
            temporary = Path(tempfile.mkdtemp(prefix="lumenite-stage-", dir=cache_dir))
            try:
                with zipfile.ZipFile(zip_path, "r") as zf:
                    for info in zf.infolist():
                        if info.is_dir():
                            continue
                        name: str = info.filename.replace("\\", "/")
                        parts: list[str] = name.split("/", 1)
                        if len(parts) < 2:
                            continue
                        rel: str = parts[1]
                        if rel.startswith("Shaders/"):
                            dest_rel: str = "reshade-shaders/Shaders/" + rel[len("Shaders/") :]
                        elif rel.startswith("Textures/"):
                            dest_rel = "reshade-shaders/Textures/" + rel[len("Textures/") :]
                        else:
                            continue
                        target = safe_archive_destination(temporary, dest_rel)
                        atomic_write_bytes(target, zf.read(info))
                atomic_write_text(temporary / ".complete", revision)
                if staging.exists():
                    shutil.rmtree(staging)
                temporary.replace(staging)
            finally:
                if temporary.exists():
                    shutil.rmtree(temporary, ignore_errors=True)
    out.staging_dir = staging
    out.files = [path for path in staging.rglob("*") if path.is_file() and path != marker]

    log(f"  staged {len(out.files)} LumeniteFX files.")
    return out


class ReshadeHeaders:
    def __init__(self) -> None:
        self.fxh_path: Path | None = None
        self.ui_fxh_path: Path | None = None
        self.drawtext_path: Path | None = None
        self.binaries: dict[str, BinaryInfo] = {}


def fetch_reshade_headers(log: LogFn, progress: ProgressFn | None = None, force: bool = False) -> ReshadeHeaders:
    out: ReshadeHeaders = ReshadeHeaders()
    log("Fetching standard ReShade shader headers (ReShade.fxh, ReShadeUI.fxh, DrawText.fxh)...")
    cache_dir: Path = get_cache_dir()

    branch: str = "slim"
    try:
        raw_meta: Any = http_get_json("https://api.github.com/repos/crosire/reshade-shaders")
        meta: dict[str, Any] = cast(dict[str, Any], raw_meta)
        branch = str(meta.get("default_branch", "slim"))
    except Exception:
        branch = "slim"

    revision = _branch_revision("crosire/reshade-shaders", branch)
    base_url: str = f"https://raw.githubusercontent.com/crosire/reshade-shaders/{branch}/Shaders/"
    for fname in ["ReShade.fxh", "ReShadeUI.fxh", "DrawText.fxh"]:
        dest: Path = cache_dir / fname
        url: str = base_url + fname
        _cached_download(dest, url, revision, progress, force or revision == branch)
        setattr(out, fname.lower().replace(".", "_") + ("_path" if not fname.lower().endswith("path") else ""), dest)
        out.binaries[fname] = BinaryInfo(
            name=fname,
            version=revision,
            sha256=sha256_file(dest),
            size_bytes=dest.stat().st_size,
            source_url=url,
        )

    out.fxh_path = cache_dir / "ReShade.fxh"
    out.ui_fxh_path = cache_dir / "ReShadeUI.fxh"
    out.drawtext_path = cache_dir / "DrawText.fxh"
    return out


class DgvoodooBundle:
    def __init__(self) -> None:
        self.version: str = ""
        self.d3d9_dll: Path | None = None
        self.conf: Path | None = None
        self.cpl: Path | None = None
        self.binaries: dict[str, BinaryInfo] = {}


def fetch_dgvoodoo(
    log: LogFn,
    progress: ProgressFn | None = None,
    force: bool = False,
    architecture: str = "x86",
) -> DgvoodooBundle:
    out: DgvoodooBundle = DgvoodooBundle()
    if architecture not in {"x86", "x64"}:
        raise ValueError(f"Unsupported dgVoodoo architecture: {architecture}")
    log("Checking github.com/dege-diosg/dgVoodoo2 for the latest release...")
    cache_dir: Path = get_cache_dir()

    zip_url: str = ""
    zip_name: str = "dgVoodoo2_87_3.zip"
    try:
        raw_j: Any = http_get_json("https://api.github.com/repos/dege-diosg/dgVoodoo2/releases/latest")
        j: dict[str, Any] = cast(dict[str, Any], raw_j)
        out.version = str(j.get("tag_name", ""))
        assets: list[dict[str, Any]] = cast(list[dict[str, Any]], j.get("assets", []))
        for a in assets:
            name: str = str(a.get("name", ""))
            lname: str = name.lower()
            if lname.startswith("dgvoodoo2_") and lname.endswith(".zip") and "dbg" not in lname and "dev" not in lname:
                zip_name = name
                zip_url = str(a.get("browser_download_url", ""))
                break
    except Exception:
        pass

    if not zip_url:
        zip_url = f"https://github.com/dege-diosg/dgVoodoo2/releases/latest/download/{zip_name}"

    zip_path: Path = cache_dir / zip_name
    revision = out.version or zip_name
    _cached_download(zip_path, zip_url, revision, progress, force or not out.version)

    stage = cache_dir / f"dgvoodoo_{architecture}"
    extracted_d3d9 = zip_extract_matching(zip_path, stage, [f"MS/{architecture}/D3D9.dll"], flatten=True)[0]
    extracted_conf = zip_extract_matching(zip_path, stage, ["dgVoodoo.conf"], flatten=True)[0]
    extracted_cpl = zip_extract_matching(zip_path, stage, ["dgVoodooCpl.exe"], flatten=True)[0]

    out.d3d9_dll = extracted_d3d9
    out.conf = extracted_conf
    out.cpl = extracted_cpl
    for path in [extracted_d3d9, extracted_conf, extracted_cpl]:
        out.binaries[path.name] = BinaryInfo(
            name=path.name,
            version=revision,
            sha256=sha256_file(path),
            size_bytes=path.stat().st_size,
            source_url=zip_url,
        )
    return out
