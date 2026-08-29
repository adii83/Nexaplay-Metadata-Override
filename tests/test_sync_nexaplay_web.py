import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

SCRIPT_DIR = Path(__file__).parents[1] / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))
import sync_nexaplay_web as sync


class SyncNexaplayWebTests(unittest.TestCase):
    def test_workflow_contract(self):
        workflow = Path(__file__).parents[1] / ".github" / "workflows" / "sync-nexaplay-web.yml"
        text = workflow.read_text(encoding="utf-8")

        for required in (
            "new_games.json",
            "nexaplay_override.json",
            "scripts/sync_nexaplay_web.py",
            "tests/test_sync_nexaplay_web.py",
            ".github/workflows/sync-nexaplay-web.yml",
            "python -m unittest discover -s tests -p 'test_*.py' -v",
            'cron: "17 2 * * *"',
            "workflow_dispatch:",
            "dry_run:",
            "default: true",
            "concurrency:",
            "group: nexaplay-web-catalog-sync",
            "cancel-in-progress: false",
            "permissions:",
            "contents: read",
            "actions/checkout@v4",
            "repository: adii83/Nexaplay-Web",
            "token: ${{ secrets.NEXAPLAY_WEB_TOKEN }}",
            "R2_ACCOUNT_ID",
            "R2_ACCESS_KEY_ID",
            "R2_SECRET_ACCESS_KEY",
            "R2_BUCKET_NAME",
            "NEXAPLAY_WEB_TOKEN",
            "aws s3api list-objects-v2",
            "METADATA_BASE_URL: https://meta.nexaplaymetadata.online/Metadata",
            "sync_nexaplay_web.py",
            "web_catalog_builder/games_list.json",
            "web_catalog_builder/output/search_index.json",
            "web_catalog_builder/output/overrides.json",
            "web_catalog_builder/output/r2_manifest.json",
            "git pull --rebase origin main",
            "git push origin HEAD:main",
        ):
            self.assertIn(required, text)
        for forbidden in ("push -f", "push --force", "--force-with-lease", "reset --hard", "--no-paginate"):
            self.assertNotIn(forbidden, text)

    def write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")

    def test_classifies_new_game_price_rule(self):
        self.assertFalse(sync.classify_new_game({"store_data": {"price_overview": {"initial": 239}}}, {}))
        self.assertTrue(sync.classify_new_game({"store_data": {"price_overview": {"initial": 999}}}, {}))
        self.assertFalse(sync.classify_new_game({"store_data": {"is_free": True, "price_overview": {"initial": 9999}}}, {}))
        self.assertFalse(sync.classify_new_game({"store_data": {}}, {}))
        self.assertTrue(sync.classify_new_game(
            {"store_data": {"price_overview": {"initial": 1}}},
            {"catalog": {"price_normalized": 135000}},
        ))
        self.assertFalse(sync.classify_new_game(
            {"store_data": {"price_overview": {"initial": 1}}},
            {"catalog": {"price_normalized": 134999}},
        ))
        with self.assertRaises(sync.SyncError):
            sync.classify_new_game({"store_data": {"price_overview": {"initial": "999"}}}, {})

    def test_plan_sync_baseline_and_etag_retry(self):
        listing = {
            100: {"etag": "old", "last_modified": "t1"},
            200: {"etag": "new", "last_modified": "t2"},
            300: {"etag": "unknown", "last_modified": "t3"},
        }
        baseline = sync.plan_sync(listing, {}, {100}, {200}, {200})
        self.assertTrue(baseline.baseline)
        self.assertEqual(baseline.new_appids, (200,))
        self.assertEqual(baseline.changed_existing_appids, ())
        self.assertEqual(baseline.override_appids, (200,))
        self.assertEqual(baseline.current_objects[100]["etag"], "old")

        manifest = {"version": 1, "objects": {"100": {"etag": "old", "last_modified": "t0"}, "400": {"etag": "gone", "last_modified": "t0"}}}
        changed = sync.plan_sync(listing, manifest, {100}, set(), set())
        self.assertFalse(changed.baseline)
        self.assertEqual(changed.changed_existing_appids, ())
        self.assertEqual(changed.removed_manifest_appids, (400,))

        listing[100]["etag"] = "changed"
        retry = sync.plan_sync(listing, manifest, {100}, set(), set())
        self.assertEqual(retry.changed_existing_appids, (100,))
        self.assertNotIn(300, retry.changed_existing_appids)

    def test_load_unique_appids_rejects_duplicates(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "new_games.json"
            self.write_json(path, [1, 1])
            with self.assertRaises(sync.SyncError):
                sync.load_unique_appids(path)

    def test_parse_r2_listing_ignores_noncanonical_metadata_keys(self):
        listing = sync.parse_r2_listing({"Contents": [
            {"Key": "Metadata/10.json", "ETag": '"valid"', "LastModified": "t"},
            {"Key": "Metadata/archive/10.json", "ETag": '"nested"'},
            {"Key": "Metadata/README.json", "ETag": '"readme"'},
            {"Key": "Metadata/10.backup.json", "ETag": '"backup"'},
            {"Key": "Metadata/01.json", "ETag": '"leading-zero"'},
        ]})

        self.assertEqual(listing, {10: {"etag": "valid", "last_modified": "t"}})

    def test_synchronize_normalizes_mixed_master_appids(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, web, catalog = root / "source", root / "web", root / "web" / "web_catalog_builder"
            self.write_json(source / "new_games.json", [3768760])
            self.write_json(source / "nexaplay_override.json", {})
            self.write_json(catalog / "games_list.json", [
                {"appid": "3768760", "title": "String ID", "cover_url": "string-cover", "premium": True, "publisher": "Keep"},
                {"appid": 20, "title": "Integer ID", "cover_url": "twenty", "premium": False},
            ])
            self.write_json(catalog / "output" / "search_index.json", [[3768760, "String ID", 1, "string-cover"], [20, "Integer ID", 0, "twenty"]])
            self.write_json(catalog / "output" / "r2_manifest.json", {"version": 1, "objects": {}})
            self.write_json(root / "listing.json", {"Contents": []})

            with patch.object(sync.subprocess, "run", side_effect=self.build_index):
                result = sync.synchronize(sync.SyncConfig(source, web, root / "listing.json", "unused"), lambda _: self.fail("unexpected fetch"))

            games = json.loads((catalog / "games_list.json").read_text(encoding="utf-8"))
            self.assertEqual(result.added_count, 0)
            self.assertEqual([game["appid"] for game in games], [3768760, 20])
            self.assertEqual(games[0]["premium"], True)
            self.assertEqual(games[0]["publisher"], "Keep")

    def test_master_appids_reject_invalid_and_normalized_duplicates(self):
        for appids in ((True,), ("01",), ("0",), ("10", 10)):
            games = [{"appid": appid, "title": str(appid), "cover_url": "cover", "premium": False} for appid in appids]
            with self.subTest(appids=appids), self.assertRaises(sync.SyncError):
                sync.normalize_games(games)

    def test_synchronize_merges_and_preserves_existing_invariants(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "source"
            web = root / "web"
            catalog = web / "web_catalog_builder"
            self.write_json(source / "new_games.json", [3751950])
            self.write_json(source / "nexaplay_override.json", {
                "3751950": {"catalog": {"title": "Override title"}},
                "10": {"catalog": {"developers": ["Dev override"]}},
            })
            self.write_json(catalog / "games_list.json", [{
                "appid": 10, "title": "Old", "cover_url": "old-cover", "header": "old-header",
                "premium": True, "publisher": "R2 pub", "genres": ["Action"],
            }])
            self.write_json(catalog / "output" / "search_index.json", [[10, "Old", 1, "old-cover"]])
            self.write_json(catalog / "output" / "r2_manifest.json", {"version": 1, "objects": {"10": {"etag": "old", "last_modified": "t"}}})
            listing = root / "listing.json"
            self.write_json(listing, {"Contents": [
                {"Key": "Metadata/10.json", "ETag": "\"changed\"", "LastModified": "t2"},
                {"Key": "Metadata/3751950.json", "ETag": "\"new\"", "LastModified": "t2"},
            ]})
            data = {
                10: {"steam_appid": "10", "name": "Refreshed", "assets": {"header": [{"url": "r2-header"}]}, "store_data": {}},
                3751950: {"steam_appid": "242050", "name": "R2 title", "assets": {"library_capsule": [{"url": "new-cover"}]}, "store_data": {"price_overview": {"initial": 999}}},
            }
            config = sync.SyncConfig(source, web, listing, "https://unused")
            with patch.object(sync.subprocess, "run", side_effect=self.build_index):
                result = sync.synchronize(config, data.__getitem__)
            games = json.loads((catalog / "games_list.json").read_text(encoding="utf-8"))
            by_id = {item["appid"]: item for item in games}
            self.assertEqual(result.added_count, 1)
            self.assertEqual(by_id[3751950]["title"], "Override title")
            self.assertEqual(by_id[3751950]["cover_url"], "new-cover")
            self.assertTrue(by_id[3751950]["premium"])
            self.assertEqual(by_id[10]["premium"], True)
            self.assertEqual(by_id[10]["publisher"], "R2 pub")
            self.assertEqual(by_id[10]["genres"], ["Action"])

    def test_first_baseline_records_all_r2_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, web, catalog = root / "source", root / "web", root / "web" / "web_catalog_builder"
            self.write_json(source / "new_games.json", [])
            self.write_json(source / "nexaplay_override.json", {})
            self.write_json(catalog / "games_list.json", [{"appid": 10, "title": "Old", "cover_url": "old", "premium": False}])
            self.write_json(catalog / "output" / "search_index.json", [[10, "Old", 0, "old"]])
            listing = root / "listing.json"
            self.write_json(listing, {"Contents": [
                {"Key": "Metadata/10.json", "ETag": "\"catalog\"", "LastModified": "t1"},
                {"Key": "Metadata/99.json", "ETag": "\"outside\"", "LastModified": "t2"},
            ]})
            with patch.object(sync.subprocess, "run", side_effect=self.build_index):
                sync.synchronize(sync.SyncConfig(source, web, listing, "unused"), lambda _: self.fail("unexpected fetch"))
            manifest = json.loads((catalog / "output" / "r2_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(set(manifest["objects"]), {"10", "99"})

    def test_failed_baseline_override_is_not_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, web, catalog = root / "source", root / "web", root / "web" / "web_catalog_builder"
            self.write_json(source / "new_games.json", [])
            self.write_json(source / "nexaplay_override.json", {"10": {"catalog": {"title": "Override"}}})
            self.write_json(catalog / "games_list.json", [{"appid": 10, "title": "Old", "cover_url": "old", "premium": False}])
            self.write_json(catalog / "output" / "search_index.json", [[10, "Old", 0, "old"]])
            listing = root / "listing.json"
            self.write_json(listing, {"Contents": [
                {"Key": "Metadata/10.json", "ETag": "\"retry\"", "LastModified": "t1"},
                {"Key": "Metadata/99.json", "ETag": "\"outside\"", "LastModified": "t2"},
            ]})
            with patch.object(sync.subprocess, "run", side_effect=self.build_index):
                result = sync.synchronize(sync.SyncConfig(source, web, listing, "unused"), lambda _: (_ for _ in ()).throw(ValueError("bad json")))
            manifest = json.loads((catalog / "output" / "r2_manifest.json").read_text(encoding="utf-8"))
            self.assertNotIn("10", manifest["objects"])
            self.assertEqual(manifest["objects"]["99"]["etag"], "outside")
            self.assertEqual(len(result.warnings), 1)

    def test_invalid_new_metadata_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, web, catalog = root / "source", root / "web", root / "web" / "web_catalog_builder"
            self.write_json(source / "new_games.json", [20])
            self.write_json(source / "nexaplay_override.json", {})
            original = [{"appid": 10, "title": "Old", "cover_url": "old", "premium": False}]
            self.write_json(catalog / "games_list.json", original)
            self.write_json(catalog / "output" / "search_index.json", [[10, "Old", 0, "old"]])
            self.write_json(catalog / "output" / "r2_manifest.json", {"version": 1, "objects": {}})
            listing = root / "listing.json"
            self.write_json(listing, {"Contents": [{"Key": "Metadata/20.json", "ETag": "\"new\"", "LastModified": "t"}]})
            with self.assertRaises(sync.SyncError):
                sync.synchronize(sync.SyncConfig(source, web, listing, "https://unused"), lambda _: {})
            self.assertEqual(json.loads((catalog / "games_list.json").read_text(encoding="utf-8")), original)

    def test_existing_refresh_failure_keeps_old_etag_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, web, catalog = root / "source", root / "web", root / "web" / "web_catalog_builder"
            self.write_json(source / "new_games.json", [])
            self.write_json(source / "nexaplay_override.json", {})
            self.write_json(catalog / "games_list.json", [{"appid": 10, "title": "Old", "cover_url": "old", "premium": False}])
            self.write_json(catalog / "output" / "search_index.json", [[10, "Old", 0, "old"]])
            self.write_json(catalog / "output" / "r2_manifest.json", {"version": 1, "objects": {"10": {"etag": "old", "last_modified": "t0"}}})
            listing = root / "listing.json"
            self.write_json(listing, {"Contents": [{"Key": "Metadata/10.json", "ETag": "\"changed\"", "LastModified": "t1"}]})
            with patch.object(sync.subprocess, "run", side_effect=self.build_index):
                result = sync.synchronize(sync.SyncConfig(source, web, listing, "https://unused"), lambda _: (_ for _ in ()).throw(ValueError("bad json")))
            manifest = json.loads((catalog / "output" / "r2_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["objects"]["10"]["etag"], "old")
            self.assertEqual(len(result.warnings), 1)

    def test_unchanged_override_skips_fetch_but_changed_and_baseline_fetch(self):
        cases = (
            ({"version": 1, "objects": {"10": {"etag": "same", "last_modified": "t0"}}}, "Same", "Same", 0),
            ({"version": 1, "objects": {"10": {"etag": "same", "last_modified": "t0"}}}, "Old", "New", 1),
            ({}, "Same", "Same", 1),
        )
        for manifest, previous_title, current_title, expected_fetches in cases:
            with self.subTest(manifest=manifest, previous=previous_title, current=current_title), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source, web, catalog = root / "source", root / "web", root / "web" / "web_catalog_builder"
                self.write_json(source / "new_games.json", [])
                self.write_json(source / "nexaplay_override.json", {"10": {"catalog": {"title": current_title}}})
                self.write_json(catalog / "games_list.json", [{"appid": 10, "title": previous_title, "cover_url": "old", "premium": False}])
                self.write_json(catalog / "output" / "search_index.json", [[10, previous_title, 0, "old"]])
                self.write_json(catalog / "output" / "overrides.json", {"10": {"catalog": {"title": previous_title}}})
                if manifest:
                    self.write_json(catalog / "output" / "r2_manifest.json", manifest)
                self.write_json(root / "listing.json", {"Contents": [{"Key": "Metadata/10.json", "ETag": '"same"', "LastModified": "t1"}]})
                fetches = []

                def fetch(appid):
                    fetches.append(appid)
                    return {"name": "R2", "assets": {"header": [{"url": "cover"}]}, "store_data": {}}

                with patch.object(sync.subprocess, "run", side_effect=self.build_index):
                    sync.synchronize(sync.SyncConfig(source, web, root / "listing.json", "unused"), fetch)
                self.assertEqual(len(fetches), expected_fetches)

    def test_failed_changed_active_override_preserves_previous_projection_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, web, catalog = root / "source", root / "web", root / "web" / "web_catalog_builder"
            self.write_json(source / "new_games.json", [])
            self.write_json(source / "nexaplay_override.json", {"10": {"catalog": {"title": "New override"}}})
            self.write_json(catalog / "games_list.json", [{"appid": 10, "title": "Old override", "cover_url": "old", "premium": False}])
            self.write_json(catalog / "output" / "search_index.json", [[10, "Old override", 0, "old"]])
            previous = {"10": {"catalog": {"title": "Old override"}}}
            self.write_json(catalog / "output" / "overrides.json", previous)
            self.write_json(catalog / "output" / "r2_manifest.json", {"version": 1, "objects": {"10": {"etag": "same", "last_modified": "t0"}}})
            self.write_json(root / "listing.json", {"Contents": [{"Key": "Metadata/10.json", "ETag": '"same"', "LastModified": "t1"}]})
            config = sync.SyncConfig(source, web, root / "listing.json", "unused")

            with patch.object(sync.subprocess, "run", side_effect=self.build_index):
                result = sync.synchronize(config, lambda _: (_ for _ in ()).throw(ValueError("bad json")))
            self.assertEqual(json.loads((catalog / "output" / "overrides.json").read_text(encoding="utf-8")), previous)
            self.assertEqual(len(result.warnings), 1)

            fetches = []
            def fetch(appid):
                fetches.append(appid)
                return {"name": "R2", "assets": {"header": [{"url": "cover"}]}, "store_data": {}}

            with patch.object(sync.subprocess, "run", side_effect=self.build_index):
                sync.synchronize(config, fetch)
            self.assertEqual(fetches, [10])

    def test_removed_override_refreshes_materialized_fields_from_r2(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, web, catalog = root / "source", root / "web", root / "web" / "web_catalog_builder"
            self.write_json(source / "new_games.json", [])
            self.write_json(source / "nexaplay_override.json", {})
            self.write_json(catalog / "games_list.json", [{"appid": 10, "title": "Override title", "cover_url": "override-cover", "premium": False}])
            self.write_json(catalog / "output" / "search_index.json", [[10, "Override title", 0, "override-cover"]])
            self.write_json(catalog / "output" / "overrides.json", {"10": {"catalog": {"title": "Override title", "header": "override-cover"}}})
            self.write_json(catalog / "output" / "r2_manifest.json", {"version": 1, "objects": {"10": {"etag": "same", "last_modified": "t0"}}})
            listing = root / "listing.json"
            self.write_json(listing, {"Contents": [{"Key": "Metadata/10.json", "ETag": "\"same\"", "LastModified": "t1"}]})
            data = {"name": "R2 title", "assets": {"header": [{"url": "r2-cover"}]}, "store_data": {}}
            with patch.object(sync.subprocess, "run", side_effect=self.build_index):
                sync.synchronize(sync.SyncConfig(source, web, listing, "unused"), lambda _: data)
            game = json.loads((catalog / "games_list.json").read_text(encoding="utf-8"))[0]
            self.assertEqual((game["title"], game["cover_url"]), ("R2 title", "r2-cover"))
            self.assertEqual(json.loads((catalog / "output" / "overrides.json").read_text(encoding="utf-8")), {})

    def test_failed_removed_override_refresh_clears_manifest_for_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, web, catalog = root / "source", root / "web", root / "web" / "web_catalog_builder"
            self.write_json(source / "new_games.json", [])
            self.write_json(source / "nexaplay_override.json", {})
            self.write_json(catalog / "games_list.json", [{"appid": 10, "title": "Override title", "cover_url": "override-cover", "premium": False}])
            self.write_json(catalog / "output" / "search_index.json", [[10, "Override title", 0, "override-cover"]])
            self.write_json(catalog / "output" / "overrides.json", {"10": {"catalog": {"title": "Override title"}}})
            self.write_json(catalog / "output" / "r2_manifest.json", {"version": 1, "objects": {"10": {"etag": "same", "last_modified": "t0"}}})
            listing = root / "listing.json"
            self.write_json(listing, {"Contents": [{"Key": "Metadata/10.json", "ETag": "\"same\"", "LastModified": "t1"}]})
            with patch.object(sync.subprocess, "run", side_effect=self.build_index):
                result = sync.synchronize(sync.SyncConfig(source, web, listing, "unused"), lambda _: (_ for _ in ()).throw(ValueError("bad json")))
            manifest = json.loads((catalog / "output" / "r2_manifest.json").read_text(encoding="utf-8"))
            self.assertNotIn("10", manifest["objects"])
            self.assertEqual(len(result.warnings), 1)

    def test_rollback_restores_all_outputs_on_replace_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, web = Path(tmp), Path(tmp) / "source", Path(tmp) / "web"
            catalog = web / "web_catalog_builder"
            self.write_json(source / "new_games.json", [20])
            self.write_json(source / "nexaplay_override.json", {})
            self.write_json(catalog / "games_list.json", [{"appid": 10, "title": "Old", "cover_url": "old", "premium": False}])
            self.write_json(catalog / "output" / "search_index.json", [[10, "Old", 0, "old"]])
            self.write_json(catalog / "output" / "overrides.json", {"old": {}})
            self.write_json(catalog / "output" / "r2_manifest.json", {"version": 1, "objects": {}})
            self.write_json(root / "listing.json", {"Contents": [{"Key": "Metadata/20.json", "ETag": "\"new\"", "LastModified": "t"}]})
            originals = {path: path.read_bytes() for path in (catalog / "games_list.json", catalog / "output" / "search_index.json", catalog / "output" / "overrides.json", catalog / "output" / "r2_manifest.json")}
            data = {"name": "New", "assets": {"header": [{"url": "cover"}]}, "store_data": {}}
            original_replace = Path.replace
            replace_calls = []
            forward_count = {"n": 0}
            def fail_third_forward(self, target):
                replace_calls.append((self, Path(target)))
                if ".rollback" not in self.name:
                    forward_count["n"] += 1
                    if forward_count["n"] == 3:
                        raise OSError("injected replace failure")
                return original_replace(self, target)
            original_write_bytes = Path.write_bytes
            def forbid_destination_write(self, content):
                if self in originals:
                    raise AssertionError("rollback must use replace")
                return original_write_bytes(self, content)
            with patch.object(sync.subprocess, "run", side_effect=self.build_index), patch.object(Path, "replace", fail_third_forward), patch.object(Path, "write_bytes", forbid_destination_write):
                with self.assertRaises(OSError):
                    sync.synchronize(sync.SyncConfig(source, web, root / "listing.json", "unused"), lambda _: data)
            for path, content in originals.items():
                self.assertEqual(path.read_bytes(), content)
            restored = {target for source_path, target in replace_calls if ".rollback" in source_path.name}
            self.assertEqual(restored, {catalog / "games_list.json", catalog / "output" / "search_index.json"})
            self.assertFalse(list(catalog.rglob("*.rollback")))

    def test_keyboard_interrupt_during_replace_restores_outputs(self):
        for interrupt_at in (2, 3):
            with self.subTest(interrupt_at=interrupt_at), tempfile.TemporaryDirectory() as tmp:
                root, source, web = Path(tmp), Path(tmp) / "source", Path(tmp) / "web"
                catalog = web / "web_catalog_builder"
                self.write_json(source / "new_games.json", [20])
                self.write_json(source / "nexaplay_override.json", {})
                self.write_json(catalog / "games_list.json", [{"appid": 10, "title": "Old", "cover_url": "old", "premium": False}])
                self.write_json(catalog / "output" / "search_index.json", [[10, "Old", 0, "old"]])
                self.write_json(catalog / "output" / "overrides.json", {})
                self.write_json(catalog / "output" / "r2_manifest.json", {"version": 1, "objects": {}})
                self.write_json(root / "listing.json", {"Contents": [{"Key": "Metadata/20.json", "ETag": '"new"', "LastModified": "t"}]})
                outputs = (catalog / "games_list.json", catalog / "output" / "search_index.json", catalog / "output" / "overrides.json", catalog / "output" / "r2_manifest.json")
                originals = {path: path.read_bytes() for path in outputs}
                data = {"name": "New", "assets": {"header": [{"url": "cover"}]}, "store_data": {}}
                original_replace = Path.replace
                forward_count = 0

                def interrupt_forward(path, target):
                    nonlocal forward_count
                    if not path.name.endswith(".rollback"):
                        forward_count += 1
                        if forward_count == interrupt_at:
                            raise KeyboardInterrupt
                    return original_replace(path, target)

                with patch.object(sync.subprocess, "run", side_effect=self.build_index), patch.object(Path, "replace", interrupt_forward):
                    with self.assertRaises(KeyboardInterrupt):
                        sync.synchronize(sync.SyncConfig(source, web, root / "listing.json", "unused"), lambda _: data)

                for path, content in originals.items():
                    self.assertEqual(path.read_bytes(), content)
                self.assertFalse(list(catalog.rglob("*.rollback")))

    def test_failed_rollback_restore_preserves_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, web = Path(tmp), Path(tmp) / "source", Path(tmp) / "web"
            catalog = web / "web_catalog_builder"
            self.write_json(source / "new_games.json", [20])
            self.write_json(source / "nexaplay_override.json", {})
            self.write_json(catalog / "games_list.json", [{"appid": 10, "title": "Old", "cover_url": "old", "premium": False}])
            self.write_json(catalog / "output" / "search_index.json", [[10, "Old", 0, "old"]])
            self.write_json(catalog / "output" / "overrides.json", {})
            self.write_json(catalog / "output" / "r2_manifest.json", {"version": 1, "objects": {}})
            self.write_json(root / "listing.json", {"Contents": [{"Key": "Metadata/20.json", "ETag": '"new"', "LastModified": "t"}]})
            data = {"name": "New", "assets": {"header": [{"url": "cover"}]}, "store_data": {}}
            original_replace = Path.replace
            forward_count = 0

            def fail_forward_and_restore(path, target):
                nonlocal forward_count
                target = Path(target)
                if path.name.endswith(".rollback") and target == catalog / "games_list.json":
                    raise OSError("injected restore failure")
                if not path.name.endswith(".rollback"):
                    forward_count += 1
                    if forward_count == 3:
                        raise OSError("injected forward failure")
                return original_replace(path, target)

            with patch.object(sync.subprocess, "run", side_effect=self.build_index), patch.object(Path, "replace", fail_forward_and_restore):
                with self.assertRaisesRegex(OSError, "injected restore failure"):
                    sync.synchronize(sync.SyncConfig(source, web, root / "listing.json", "unused"), lambda _: data)

            backup = catalog / "games_list.json.rollback"
            self.assertTrue(backup.exists())
            self.assertEqual(json.loads(backup.read_text(encoding="utf-8"))[0]["title"], "Old")
            self.assertFalse((catalog / "output" / "search_index.json.rollback").exists())

    def test_overrides_only_projects_without_listing_or_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source, web = root / "source", root / "web"
            self.write_json(source / "nexaplay_override.json", {"10": {"catalog": {"title": "Only"}}})
            result = sync.synchronize(sync.SyncConfig(source, web, root / "missing.json", "https://unused", overrides_only=True), lambda _: self.fail("fetch"))
            self.assertEqual(result.override_count, 1)
            self.assertEqual(json.loads((web / "web_catalog_builder" / "output" / "overrides.json").read_text(encoding="utf-8")), {"10": {"catalog": {"title": "Only"}}})

    def test_validators_reject_bad_index_override_and_manifest(self):
        game = {"appid": 1, "title": "One", "cover_url": "cover", "premium": False}
        with self.assertRaises(sync.SyncError):
            sync.validate_games([], [game], [[1, "One", 0, "cover", 1]], {}, {"version": 1, "objects": {}})
        with self.assertRaises(sync.SyncError):
            sync.project_overrides({"1": {"catalog": {"title": 7}}})
        with self.assertRaises(sync.SyncError):
            sync.validate_manifest({"version": 1, "objects": {"x": {"etag": "e", "last_modified": "t"}}})

    def test_strict_review_validators(self):
        game = {"appid": 1, "title": "One", "cover_url": "cover", "premium": False}
        with self.assertRaises(sync.SyncError):
            sync.validate_games([], [game], [[2, "One", 0, "cover"]], {}, {"version": 1, "objects": {}})
        for override in (
            {"1": {"catalog": []}},
            {"1": {"catalog": {"price_normalized": float("nan")}}},
            {"1": {"detail": {"developers": [""]}}},
            {"1": {"detail": {"pc_requirements_minimum": 3}}},
        ):
            with self.assertRaises(sync.SyncError):
                sync.project_overrides(override)
        for manifest in (
            {"version": 1, "objects": {"01": {"etag": "e", "last_modified": "t"}}},
            {"version": 1, "objects": {"1": {"etag": "", "last_modified": "t"}}},
        ):
            with self.assertRaises(sync.SyncError):
                sync.validate_manifest(manifest)
        with self.assertRaises(sync.SyncError):
            sync.parse_r2_listing({"Contents": [{"Key": "Metadata/1.json"}, {"Key": "Metadata/1.json"}]})
        for metadata in (
            {"store_data": "bad"},
            {"store_data": {"is_free": "yes"}},
            {"store_data": {"price_overview": {"initial": float("inf")}}},
        ):
            with self.assertRaises(sync.SyncError):
                sync.classify_new_game(metadata, {})
        self.assertTrue(sync.classify_new_game({"store_data": {"price_overview": {"initial": 843.75}}}, {}))

    def test_current_shaped_override_and_metadata_url(self):
        override = {"1": {"catalog": {"library_hero_2x": "hero", "icon": "icon", "background_raw": "bg", "short_description": "short", "release_date": "2026", "price_display": "$9", "protection": True}, "detail": {"developers": ["Dev"], "about_the_game": "about", "screenshots": [], "movies": [], "support_email": "x@y.z", "pc_requirements_minimum": None}}}
        self.assertEqual(sync.project_overrides(override), override)
        self.assertEqual(sync.metadata_url("https://meta.example/Metadata/", 42), "https://meta.example/Metadata/42.json")
        for appid in ("0", "01"):
            with self.subTest(appid=appid), self.assertRaises(sync.SyncError):
                sync.project_overrides({appid: {}})

    def test_overrides_only_cli_does_not_require_metadata_base_url(self):
        result = sync.SyncResult(0, 0, 0, ("AppID 10 refresh failed: bad json",), ())
        stderr = io.StringIO()
        with patch.object(sys, "argv", ["sync", "--source-root", "source", "--web-root", "web", "--overrides-only"]), patch.object(sync, "synchronize", return_value=result), redirect_stderr(stderr):
            sync.main()
        self.assertIn("AppID 10 refresh failed: bad json", stderr.getvalue())

    @staticmethod
    def build_index(command, check):
        games = Path(command[command.index("--games-list") + 1])
        output = Path(command[command.index("--output") + 1])
        rows = json.loads(games.read_text(encoding="utf-8"))
        output.write_text(json.dumps([[x["appid"], x["title"], int(x["premium"]), x.get("cover_url", x.get("header", ""))] for x in rows]), encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
