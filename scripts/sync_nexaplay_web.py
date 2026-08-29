import argparse
import json
import math
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable


class SyncError(Exception):
    pass


@dataclass(frozen=True)
class SyncConfig:
    source_root: Path
    web_root: Path
    r2_listing_path: Path
    metadata_base_url: str
    dry_run: bool = False
    overrides_only: bool = False


@dataclass(frozen=True)
class SyncPlan:
    baseline: bool
    new_appids: tuple[int, ...]
    changed_existing_appids: tuple[int, ...]
    override_appids: tuple[int, ...]
    removed_manifest_appids: tuple[int, ...]
    current_objects: dict[int, dict[str, str]]


@dataclass(frozen=True)
class SyncResult:
    added_count: int
    refreshed_count: int
    override_count: int
    warnings: tuple[str, ...]
    changed_paths: tuple[Path, ...]


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SyncError(f"invalid JSON: {path}") from exc


def load_unique_appids(path: Path) -> list[int]:
    values = read_json(path)
    if not isinstance(values, list) or any(type(value) is not int or value <= 0 for value in values):
        raise SyncError("new_games.json must be an array of positive integer AppIDs")
    if len(values) != len(set(values)):
        raise SyncError("new_games.json contains duplicate AppIDs")
    return values


def _valid_strings(value):
    return isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value)


def _validate_mapping(value, allowed):
    if not isinstance(value, dict):
        raise SyncError("override mapping must be an object")
    for key, item in value.items():
        if key not in allowed:
            raise SyncError("unknown override field")
        if key == "price_normalized" and (not isinstance(item, (int, float)) or isinstance(item, bool) or not math.isfinite(item) or item < 0):
            raise SyncError("invalid normalized price")
        if key in {"title", "header", "library_capsule", "library_capsule_2x", "library_hero_2x", "icon", "background_raw", "short_description", "release_date", "price_display", "publisher", "developer", "about_the_game", "detailed_description", "supported_languages", "website", "background_image", "support_url", "support_email", "legal_notice", "drm_notice", "store_price_final_formatted", "store_price_currency"} and (not isinstance(item, str) or not item.strip()):
            raise SyncError("override text field must be nonempty string")
        if key == "protection" and not isinstance(item, bool):
            raise SyncError("protection must be boolean")
        if key in {"screenshots", "movies", "categories"} and not isinstance(item, list):
            raise SyncError("media field must be array")
        if key in {"developers", "publishers"} and not _valid_strings(item):
            raise SyncError("override people field must be string array")
        if key == "genre" and not ((isinstance(item, str) and item.strip()) or _valid_strings(item)):
            raise SyncError("genre must be string or string array")
        if key in {"pc_requirements_minimum", "pc_requirements_recommended"} and item is not None and not isinstance(item, str):
            raise SyncError("requirements must be string or null")


def load_override_map(path: Path) -> dict[str, dict]:
    values = read_json(path)
    if not isinstance(values, dict):
        raise SyncError("override root must be an object")
    allowed_catalog = {"title", "header", "library_capsule", "library_capsule_2x", "library_hero_2x", "icon", "background_raw", "short_description", "release_date", "price_display", "protection", "developers", "publishers", "developer", "publisher", "genre", "price_normalized"}
    allowed_detail = {"developers", "publishers", "short_description", "about_the_game", "detailed_description", "supported_languages", "website", "release_date", "screenshots", "movies", "background_image", "categories", "support_url", "support_email", "legal_notice", "drm_notice", "store_price_final_formatted", "store_price_currency", "pc_requirements_minimum", "pc_requirements_recommended"}
    for appid, override in values.items():
        if not isinstance(appid, str) or not appid.isdigit() or int(appid) <= 0 or str(int(appid)) != appid or not isinstance(override, dict):
            raise SyncError("override root has invalid AppID mapping")
        if set(override) - {"catalog", "detail"}:
            raise SyncError("invalid override section")
        if "catalog" in override:
            _validate_mapping(override["catalog"], allowed_catalog)
        if "detail" in override:
            _validate_mapping(override["detail"], allowed_detail)
    return values


def classify_new_game(metadata: dict, override: dict) -> bool:
    project_overrides({"1": override} if override else {})
    store = metadata.get("store_data")
    if store is None:
        return False
    if not isinstance(store, dict):
        raise SyncError("store_data must be an object")
    if "is_free" in store and not isinstance(store["is_free"], bool):
        raise SyncError("is_free must be boolean")
    if store.get("is_free") is True:
        return False
    catalog = override.get("catalog", {}) if isinstance(override, dict) else {}
    price = catalog.get("price_normalized") if isinstance(catalog, dict) else None
    if isinstance(price, (int, float)) and not isinstance(price, bool) and math.isfinite(price) and price >= 0:
        return price >= 135000
    overview = store.get("price_overview")
    if overview is None:
        return False
    if not isinstance(overview, dict):
        raise SyncError("price_overview must be an object")
    initial = overview.get("initial")
    if initial is None:
        return False
    if not isinstance(initial, (int, float)) or isinstance(initial, bool) or not math.isfinite(initial) or initial < 0:
        raise SyncError("initial price must be finite non-negative number")
    return initial > 0 and initial * 160 >= 135000


def first_asset(metadata: dict, name: str) -> str:
    assets = metadata.get("assets", {})
    values = assets.get(name, []) if isinstance(assets, dict) else []
    if isinstance(values, list):
        for value in values:
            if isinstance(value, dict) and isinstance(value.get("url"), str) and value["url"].strip():
                return value["url"].strip()
    return ""


def resolve_title(metadata: dict, override: dict, existing: dict | None) -> str:
    catalog = override.get("catalog", {}) if isinstance(override, dict) else {}
    choices = (catalog.get("title") if isinstance(catalog, dict) else None, metadata.get("name"), (existing or {}).get("title"))
    return next((value.strip() for value in choices if isinstance(value, str) and value.strip()), "")


def resolve_cover(metadata: dict, override: dict, existing: dict | None) -> str:
    catalog = override.get("catalog", {}) if isinstance(override, dict) else {}
    for key in ("library_capsule", "library_capsule_2x", "header"):
        value = catalog.get(key) if isinstance(catalog, dict) else None
        if isinstance(value, str) and value.strip():
            return value.strip()
    for key in ("library_capsule", "library_capsule_2x", "header"):
        value = first_asset(metadata, key)
        if value:
            return value
    old = existing or {}
    for key in ("cover_url", "header"):
        value = old.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def parse_r2_listing(payload: dict) -> dict[int, dict[str, str]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("Contents", []), list):
        raise SyncError("invalid R2 listing")
    result = {}
    for item in payload["Contents"]:
        if not isinstance(item, dict) or not isinstance(item.get("Key"), str):
            continue
        key = item["Key"]
        text = key[9:-5] if key.startswith("Metadata/") and key.endswith(".json") else ""
        if not text.isdigit() or int(text) <= 0 or str(int(text)) != text:
            continue
        appid = int(text)
        if appid in result:
            raise SyncError("duplicate R2 AppID")
        result[appid] = {"etag": str(item.get("ETag", "")).strip('"'), "last_modified": str(item.get("LastModified", ""))}
    return result


def normalize_games(games) -> list[dict]:
    if not isinstance(games, list):
        raise SyncError("master must be an array")
    result, seen = [], set()
    for item in games:
        if not isinstance(item, dict):
            raise SyncError("invalid master entry")
        value = item.get("appid")
        if type(value) is int and value > 0:
            appid = value
        elif isinstance(value, str) and value.isdigit() and int(value) > 0 and str(int(value)) == value:
            appid = int(value)
        else:
            raise SyncError("invalid master AppID")
        if appid in seen:
            raise SyncError("duplicate master AppID")
        seen.add(appid)
        result.append({**item, "appid": appid})
    return result


def project_overrides(source: dict[str, dict]) -> dict[str, dict]:
    allowed_catalog = {"title", "header", "library_capsule", "library_capsule_2x", "library_hero_2x", "icon", "background_raw", "short_description", "release_date", "price_display", "protection", "developers", "publishers", "developer", "publisher", "genre", "price_normalized"}
    allowed_detail = {"developers", "publishers", "short_description", "about_the_game", "detailed_description", "supported_languages", "website", "release_date", "screenshots", "movies", "background_image", "categories", "support_url", "support_email", "legal_notice", "drm_notice", "store_price_final_formatted", "store_price_currency", "pc_requirements_minimum", "pc_requirements_recommended"}
    for appid, value in source.items():
        if not isinstance(appid, str) or not appid.isdigit() or int(appid) <= 0 or str(int(appid)) != appid or not isinstance(value, dict):
            raise SyncError("invalid override AppID")
        for section in value:
            if section not in {"catalog", "detail"}:
                raise SyncError("invalid override projection")
            _validate_mapping(value[section], allowed_catalog if section == "catalog" else allowed_detail)
    return source


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("objects"), dict):
        raise SyncError("invalid manifest root")
    for appid, state in manifest["objects"].items():
        if not isinstance(appid, str) or not appid.isdigit() or int(appid) <= 0 or str(int(appid)) != appid or not isinstance(state, dict) or not isinstance(state.get("etag"), str) or not state["etag"] or not isinstance(state.get("last_modified"), str) or not state["last_modified"]:
            raise SyncError("invalid manifest entry")


def plan_sync(listing, manifest, catalog_appids, admitted_appids, override_appids) -> SyncPlan:
    objects = manifest.get("objects", {}) if isinstance(manifest, dict) else {}
    baseline = not isinstance(manifest, dict) or manifest.get("version") != 1
    known = {int(key): value for key, value in objects.items() if isinstance(key, str) and key.isdigit() and isinstance(value, dict)}
    new = tuple(sorted(admitted_appids - catalog_appids))
    changed = () if baseline else tuple(sorted(appid for appid in catalog_appids if appid in listing and known.get(appid, {}).get("etag") != listing[appid]["etag"]))
    overrides = tuple(sorted(override_appids & catalog_appids | (override_appids & set(new))))
    removed = tuple(sorted(set(known) - set(listing)))
    return SyncPlan(baseline, new, changed, overrides, removed, listing)


def metadata_url(base_url: str, appid: int) -> str:
    return f"{base_url.rstrip('/')}/{appid}.json"


def fetch_json(url: str, timeout=25):
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "NexaPlay-Metadata-Sync/1.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def stage_json(path: Path, value) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return temp


def atomic_json(path: Path, value):
    stage_json(path, value).replace(path)


def validate_games(old, new, index, overrides, manifest):
    old_by_id = {item["appid"]: item for item in old}
    new_by_id = {}
    for item in new:
        if not isinstance(item, dict) or type(item.get("appid")) is not int or item["appid"] <= 0 or not isinstance(item.get("title"), str) or not item["title"].strip() or not isinstance(item.get("cover_url", item.get("header")), str) or not isinstance(item.get("premium"), bool) or item["appid"] in new_by_id:
            raise SyncError("invalid master entry")
        new_by_id[item["appid"]] = item
    if not set(old_by_id) <= set(new_by_id) or any(old_by_id[appid]["premium"] != new_by_id[appid]["premium"] for appid in old_by_id):
        raise SyncError("existing catalog invariant failed")
    if len(index) != len(new) or not isinstance(overrides, dict):
        raise SyncError("generated output invariant failed")
    seen = set()
    for row in index:
        if not isinstance(row, list) or len(row) != 4 or type(row[0]) is not int or not isinstance(row[1], str) or row[2] not in (0, 1) or not isinstance(row[3], str) or row[0] in seen:
            raise SyncError("invalid search index tuple")
        seen.add(row[0])
    if seen != set(new_by_id):
        raise SyncError("search index AppID set differs from master")
    validate_manifest(manifest)


def synchronize(config: SyncConfig, fetch_metadata: Callable[[int], dict]) -> SyncResult:
    source_overrides = load_override_map(config.source_root / "nexaplay_override.json")
    catalog = config.web_root / "web_catalog_builder"
    output = catalog / "output"
    overrides_path = output / "overrides.json"
    if config.overrides_only:
        projected = project_overrides(source_overrides)
        if not config.dry_run:
            atomic_json(overrides_path, projected)
        return SyncResult(0, 0, len(projected), (), (() if config.dry_run else (overrides_path,)))
    admitted = set(load_unique_appids(config.source_root / "new_games.json"))
    games_path, index_path, manifest_path = catalog / "games_list.json", output / "search_index.json", output / "r2_manifest.json"
    old_games = normalize_games(read_json(games_path))
    old_index = read_json(index_path)
    manifest = read_json(manifest_path) if manifest_path.exists() else {}
    previous_overrides = read_json(overrides_path) if overrides_path.exists() else {}
    if not isinstance(previous_overrides, dict):
        raise SyncError("invalid previous overrides")
    previous_overrides = {
        key: value for key, value in previous_overrides.items()
        if isinstance(key, str) and key.isdigit() and int(key) > 0 and str(int(key)) == key
    }
    listing = parse_r2_listing(read_json(config.r2_listing_path))
    old_by_id = {item["appid"]: item for item in old_games}
    current_override_appids = {int(key) for key in source_overrides}
    previous_override_appids = {int(key) for key in previous_overrides if isinstance(key, str) and key.isdigit() and int(key) > 0 and str(int(key)) == key}
    removed_override_appids = previous_override_appids - current_override_appids
    affected_override_appids = {
        int(key) for key in set(source_overrides) | set(previous_overrides)
        if source_overrides.get(key) != previous_overrides.get(key)
    }
    if not isinstance(manifest, dict) or manifest.get("version") != 1:
        affected_override_appids |= current_override_appids
    plan = plan_sync(listing, manifest, set(old_by_id), admitted, affected_override_appids)
    new_games = [dict(item) for item in old_games]
    new_by_id = {item["appid"]: item for item in new_games}
    warnings, refreshed = [], 0
    succeeded = set()
    for appid in plan.new_appids:
        try:
            data = fetch_metadata(appid)
            override = source_overrides.get(str(appid), {})
            title, cover = resolve_title(data, override, None), resolve_cover(data, override, None)
            if not isinstance(data, dict) or not title or not cover:
                raise SyncError("missing required new metadata")
            new_game = {"appid": appid, "title": title, "cover_url": cover, "premium": classify_new_game(data, override)}
            new_games.append(new_game)
            new_by_id[appid] = new_game
            succeeded.add(appid)
        except Exception as exc:
            raise SyncError(f"new AppID {appid} failed") from exc
    for appid in sorted((set(plan.changed_existing_appids) | set(plan.override_appids)) - set(plan.new_appids)):
        existing = new_by_id[appid]
        try:
            data = fetch_metadata(appid)
            override = source_overrides.get(str(appid), {})
            title, cover = resolve_title(data, override, existing), resolve_cover(data, override, existing)
            if not isinstance(data, dict) or not title or not cover:
                raise SyncError("missing refresh metadata")
            existing["title"], existing["cover_url"] = title, cover
            refreshed += appid in plan.changed_existing_appids
            succeeded.add(appid)
        except Exception as exc:
            if appid in plan.new_appids:
                raise
            warnings.append(f"AppID {appid} refresh failed: {exc}")
    staged_games = games_path.with_suffix(".staged.json")
    staged_index = index_path.with_suffix(".staged.json")
    staged_paths = [staged_games, staged_index]
    try:
        staged_games.parent.mkdir(parents=True, exist_ok=True)
        staged_games.write_text(json.dumps(new_games), encoding="utf-8")
        subprocess.run([sys.executable, str(catalog / "build_index.py"), "--games-list", str(staged_games), "--existing-index", str(index_path), "--output", str(staged_index)], check=True)
        new_index = read_json(staged_index)
        next_objects = ({str(appid): state for appid, state in listing.items()} if plan.baseline else dict(manifest.get("objects", {})))
        for appid in plan.removed_manifest_appids:
            next_objects.pop(str(appid), None)
        failed_processed = (set(plan.changed_existing_appids) | set(plan.override_appids)) - succeeded
        for appid in failed_processed:
            if plan.baseline or appid in removed_override_appids:
                next_objects.pop(str(appid), None)
        for appid in succeeded:
            if appid in listing:
                next_objects[str(appid)] = listing[appid]
        next_manifest = {"version": 1, "objects": next_objects}
        projected = dict(project_overrides(source_overrides))
        for appid in failed_processed & current_override_appids:
            key = str(appid)
            if key in previous_overrides:
                projected[key] = previous_overrides[key]
            else:
                projected.pop(key, None)
        validate_games(old_games, new_games, new_index, projected, next_manifest)
        paths = (games_path, index_path, overrides_path, manifest_path)
        if config.dry_run:
            paths = ()
        else:
            staged_overrides = stage_json(overrides_path, projected)
            staged_manifest = stage_json(manifest_path, next_manifest)
            staged_paths += [staged_overrides, staged_manifest]
            replacements = ((staged_games, games_path), (staged_index, index_path), (staged_overrides, overrides_path), (staged_manifest, manifest_path))
            rollbacks = {}
            for _, destination in replacements:
                if destination.exists():
                    rollback = destination.with_suffix(destination.suffix + ".rollback")
                    rollback.write_bytes(destination.read_bytes())
                    rollbacks[destination] = rollback
                    staged_paths.append(rollback)
            changed = []
            try:
                for staged, destination in replacements:
                    staged.replace(destination)
                    changed.append(destination)
            except BaseException as original_error:
                restore_errors = []
                for destination in changed:
                    rollback = rollbacks.get(destination)
                    try:
                        if rollback is None:
                            destination.unlink(missing_ok=True)
                        else:
                            rollback.replace(destination)
                    except BaseException as restore_error:
                        restore_errors.append(restore_error)
                        if rollback in staged_paths:
                            staged_paths.remove(rollback)
                if restore_errors:
                    raise restore_errors[0] from original_error
                raise
    finally:
        for path in staged_paths:
            path.unlink(missing_ok=True)
    return SyncResult(len(plan.new_appids), refreshed, len(projected), tuple(warnings), paths)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--web-root", type=Path, required=True)
    parser.add_argument("--r2-listing", type=Path)
    parser.add_argument("--metadata-base-url")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--overrides-only", action="store_true")
    args = parser.parse_args()
    if not args.overrides_only and (args.r2_listing is None or not args.metadata_base_url):
        parser.error("--r2-listing and --metadata-base-url are required unless --overrides-only")
    config = SyncConfig(args.source_root, args.web_root, args.r2_listing or Path(), args.metadata_base_url or "", args.dry_run, args.overrides_only)
    result = synchronize(config, lambda appid: fetch_json(metadata_url(args.metadata_base_url, appid)))
    for warning in result.warnings:
        print(warning, file=sys.stderr)
    print(f"added={result.added_count} refreshed={result.refreshed_count} overrides={result.override_count}")


if __name__ == "__main__":
    main()
