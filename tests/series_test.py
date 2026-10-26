"""TV identity, episode versions, startup migration and visibility regressions."""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from unittest.mock import patch

TMP = tempfile.mkdtemp(prefix="filmax-series-")
os.environ.update({"FILMAX_DATA_DIR": TMP, "FILMAX_ENV_FILE": TMP + "/none.env",
                   "AUTH_ENABLED": "true", "AUTH_PASSWORD": "test-password", "VIEWER_PASSWORD": "viewer-password", "MSSQL_HOST": "",
                   "TMDB_API_KEY": "", "AUTO_SCAN_ON_START": "false",
                   "FTP_HOST": "127.0.0.1", "FTP_PORT": "1"})
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import acl, auth, db, episodes, ftpclient, scanner, series, users
from app.config import LibraryRoot
from app.main import app
from app.scraper import tmdb
from starlette.testclient import TestClient

db.init_db()
users.init()


class SeriesTests(unittest.TestCase):
    def setUp(self):
        with db.tx() as c:
            c.execute("DELETE FROM media_file")
            c.execute("DELETE FROM episode")
            c.execute("DELETE FROM media_item")
        for rule in acl.rules():
            acl.remove_rule(rule.id)
        acl.invalidate()
        scanner._cancel.clear()
        scanner.status.__init__()
        self.client = TestClient(app, client=("127.0.0.1", 1))
        self.client.cookies.set(auth.COOKIE, auth.make_token(auth.ADMIN))

    def item(self, item_id, key, identity=108978, state="ok", kind="tv", updated=1, title="Reacher"):
        db.execute("INSERT INTO media_item(id,kind,title,sort_title,tmdb_id,guess_key,scrape_state,genres,added_at,updated_at) "
                   "VALUES(?,?,?,?,?,?,?,?,1,?)",
                   (item_id, kind, title, title.lower(), identity, key, state, '["Drama"]', updated))

    def file(self, item_id, season, ep, filename=None, folder="/shows"):
        row = db.q1("SELECT id FROM episode WHERE item_id=? AND season=? AND episode=?", (item_id, season, ep))
        eid = row["id"] if row else db.execute("INSERT INTO episode(item_id,season,episode) VALUES(?,?,?)",
                                              (item_id, season, ep)).lastrowid
        filename = filename or f"Reacher.S{season:02}E{ep:02}.mp4"
        return db.execute("INSERT INTO media_file(item_id,episode_id,ftp_path,filename,size,duration,probe_state,play_mode,video_codec,audio_codec) "
                          "VALUES(?,?,?,?,100,600,'ok','direct','h264','aac')",
                          (item_id, eid, folder + "/" + filename, filename)).lastrowid

    def listing(self, **params):
        response = self.client.get("/api/library", params=params)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_startup_reacher_migration_and_backup(self):
        self.item(7, "tv::神隱任務")
        self.item(8, "tv::reacher", state="manual")
        for season in range(1, 5):
            for ep in range(1, 9):
                self.file(8 if season == 3 else 7, season, ep)
        files = [dict(r) for r in db.q("SELECT id,ftp_path FROM media_file ORDER BY id")]
        before = set(Path(TMP).glob("backups/*.db"))
        db.init_db()
        self.assertEqual([r["id"] for r in db.q("SELECT id FROM media_item")], [8])
        self.assertEqual([dict(r) for r in db.q("SELECT id,ftp_path FROM media_file ORDER BY id")], files)
        it = self.listing()["items"][0]
        self.assertEqual((it["file_count"], it["episode_count"], it["season_count"]), (32, 32, 4))
        self.assertEqual(episodes.next_episode(8, 2, 8)["season"], 3)
        backups = set(Path(TMP).glob("backups/*.db")) - before
        self.assertEqual(len(backups), 1)
        with sqlite3.connect(backups.pop()) as saved:
            self.assertEqual(saved.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(saved.execute("SELECT COUNT(*) FROM media_item").fetchone()[0], 2)
        before = set(Path(TMP).glob("backups/*.db"))
        db.init_db()
        self.assertEqual(set(Path(TMP).glob("backups/*.db")), before)
        self.assertEqual({r["guess_key"]: r["item_id"] for r in db.q("SELECT * FROM media_item_alias")},
                         {"tv::神隱任務": 8, "tv::reacher": 8})
        self.assertEqual(self.client.get("/api/play/" + str(files[0]["id"])).status_code, 200)
        self.assertEqual(self.listing(page_size=1)["total"], 1)
        self.assertEqual(self.listing(q="S03E01")["total"], 1)
        self.assertEqual(self.client.get("/api/genres").json()["genres"][0]["count"], 1)

    def test_versions_episode_metadata_progress_and_specials(self):
        self.item(1, "tv::alpha", state="manual")
        self.item(2, "tv::reacher")
        a = self.file(1, 1, 1, "Alpha.S01E01.1080p.mp4")
        b = self.file(2, 1, 1, "Reacher.S01E01.2160p.mp4")
        self.file(2, 2, 1)
        self.file(2, 0, 1)
        db.execute("UPDATE episode SET title='Custom' WHERE item_id=1")
        db.execute("UPDATE episode SET title='Remote',overview='Fill gap' WHERE item_id=2 AND season=1")
        for fid, pos in ((a, 10), (b, 20)):
            db.execute("INSERT INTO play_state(file_id,position,duration) VALUES(?,?,600)", (fid, pos))
        self.assertEqual(series.consolidate(2), 1)
        ep = db.q1("SELECT * FROM episode WHERE item_id=1 AND season=1")
        self.assertEqual((ep["title"], ep["overview"]), ("Custom", "Fill gap"))
        self.assertEqual([(r["file_id"], r["position"]) for r in db.q("SELECT * FROM play_state ORDER BY file_id")], [(a, 10), (b, 20)])
        it = self.listing()["items"][0]
        self.assertEqual((it["file_count"], it["episode_count"], it["season_count"], it["special_count"]), (4, 3, 2, 1))
        detail = self.client.get("/api/items/1").json()
        self.assertEqual([s["season"] for s in detail["seasons"]], [0, 1, 2])
        self.assertEqual(len(detail["seasons"][1]["episodes"][0]["files"]), 2)
        self.assertEqual(episodes.next_episode(1, 1, 1)["season"], 2)
        self.assertEqual(db.q1("PRAGMA foreign_key_check"), None)

    def test_aliases_survive_reparse_and_season_zero(self):
        self.item(1, "tv::alpha", state="manual")
        self.item(2, "tv::reacher")
        self.file(1, 1, 1, "Alpha.S01E01.mp4")
        self.file(2, 2, 1)
        series.consolidate(1)
        scanner._load_registry()
        root = LibraryRoot("/shows", "tv")
        for reparse in (False, True, True):
            with db.batch() as batch:
                for name in ("Alpha.S01E01.mp4", "Reacher.S02E01.mp4", "Reacher.S00E01.mp4"):
                    scanner._index_file(ftpclient.FtpEntry(name, "/shows/" + name, False, 100), root,
                                        reparse=reparse, batch=batch)
        self.assertEqual(db.q1("SELECT COUNT(*) c FROM media_item")["c"], 1)
        self.assertEqual(db.q1("SELECT COUNT(*) c FROM episode WHERE season=0")["c"], 1)
        scanner.reassign_episodes(1)
        self.assertEqual(db.q1("SELECT COUNT(*) c FROM episode WHERE season=0")["c"], 1)
        self.assertEqual(db.q1("SELECT scrape_state FROM media_item WHERE id=1")["scrape_state"], "manual")

    def test_different_or_unknown_identity_never_merges_by_name(self):
        self.item(1, "tv::one", identity=1)
        self.item(2, "tv::two", identity=2)
        self.item(3, "tv::unknown", identity=None)
        self.item(4, "movie::one::", identity=1, kind="movie")
        self.assertEqual(series.consolidate_existing(), 0)
        for item_id in range(1, 5):
            self.assertEqual(series.consolidate(item_id), item_id)
        self.assertEqual(db.q1("SELECT COUNT(*) c FROM media_item")["c"], 4)

    def test_manual_keeper_order_and_metadata_gaps(self):
        self.item(1, "tv::one", state="ok", updated=999)
        self.item(2, "tv::two", state="manual", updated=10, title="Keep me")
        self.item(3, "tv::three", state="manual", updated=20, title="Newest manual")
        db.execute("UPDATE media_item SET overview='Donor overview' WHERE id=2")
        self.assertEqual(series.consolidate(1), 3)
        row = db.q1("SELECT * FROM media_item WHERE id=3")
        self.assertEqual((row["title"], row["overview"], row["scrape_state"]), ("Newest manual", "Donor overview", "manual"))

    def test_merge_failure_rolls_back_metadata_files_and_aliases(self):
        self.item(1, "tv::one", state="manual")
        self.item(2, "tv::two", identity=42)
        fid = self.file(2, 1, 1)
        db.execute("CREATE TRIGGER refuse_series_delete BEFORE DELETE ON media_item WHEN OLD.id=2 BEGIN SELECT RAISE(ABORT,'injected merge failure'); END")
        try:
            with self.assertRaisesRegex(sqlite3.IntegrityError, "injected merge failure"):
                series.save_metadata(2, {"tmdb_id": 108978, "title": "Should roll back"})
        finally:
            db.execute("DROP TRIGGER refuse_series_delete")
        self.assertEqual(db.q1("SELECT tmdb_id FROM media_item WHERE id=2")["tmdb_id"], 42)
        self.assertEqual(db.q1("SELECT item_id FROM media_file WHERE id=?", (fid,))["item_id"], 2)
        self.assertEqual(db.q1("SELECT COUNT(*) c FROM media_item_alias")["c"], 0)
        self.assertEqual(db.q1("PRAGMA foreign_key_check"), None)

    def test_acl_counts_search_and_details_hide_unreadable_episodes(self):
        self.item(1, "tv::one", state="manual")
        self.item(2, "tv::two")
        self.file(1, 1, 1)
        self.file(2, 2, 1, "hidden-secret-name.mp4", folder="/private")
        series.consolidate(1)
        acl.add_rule("/private", "Private")
        acl.invalidate()
        denied = TestClient(app, client=("127.0.0.1", 1))
        denied.cookies.set(auth.COOKIE, auth.make_token(auth.VIEWER))
        it = denied.get("/api/library").json()["items"][0]
        self.assertEqual((it["file_count"], it["episode_count"], it["season_count"]), (1, 1, 1))
        self.assertEqual(denied.get("/api/library", params={"q": "hidden-secret"}).json()["total"], 0)
        detail = denied.get("/api/items/1").json()
        self.assertEqual([s["season"] for s in detail["seasons"]], [1])
        vault = self.listing(scope="vault")["items"][0]
        self.assertEqual((vault["file_count"], vault["episode_count"], vault["season_count"]), (1, 1, 1))
        self.assertEqual([s["season"] for s in self.client.get("/api/items/1").json()["seasons"]], [1, 2])

    def fake_tmdb(self):
        return {"id": 108978, "name": "Matched", "original_name": "Reacher", "first_air_date": "2022-01-01"}

    def test_title_correction_persists_without_a_match(self):
        self.item(1, "tv::one", identity=None, state="manual")
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                title = "Corrected title " + str(enabled)
                with patch.object(tmdb, "api_key", "test" if enabled else ""), patch.object(tmdb, "search", return_value=None):
                    response = self.client.post("/api/rescrape/1", params={"title": title})
                self.assertEqual(response.status_code, 200, response.text)
                row = db.q1("SELECT title,sort_title,scrape_state FROM media_item WHERE id=1")
                self.assertEqual((row["title"], row["sort_title"], row["scrape_state"]),
                                 (title, title.lower(), "manual"))

    def test_manual_pairing_returns_final_id_and_refreshes_registry(self):
        self.item(1, "tv::one", state="manual", updated=999999999999)
        self.item(2, "tv::two", identity=None)
        self.file(2, 1, 1)
        with patch.object(tmdb, "details", return_value=self.fake_tmdb()), patch.object(tmdb, "download_image", return_value=None):
            response = self.client.post("/api/rescrape/2", params={"tmdb_id": 108978})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["item_id"], 1)
        self.assertEqual(scanner._item_reg["tv::two"], 1)
        self.assertEqual(db.q1("SELECT title FROM media_item WHERE id=1")["title"], "Reacher")

    def test_automatic_pairing_and_stale_scrape_queue(self):
        self.item(1, "tv::one", identity=None, state="pending")
        self.item(2, "tv::two", state="manual")
        self.file(1, 1, 1)
        self.file(2, 2, 1)
        with patch.object(tmdb, "api_key", "test"), patch.object(tmdb, "search", return_value={"id": 108978}) as search, patch.object(tmdb, "details", return_value=self.fake_tmdb()), patch.object(tmdb, "download_image", return_value=None), patch.object(tmdb, "season", return_value=None):
            scanner._scrape_items(force=True)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(db.q1("SELECT COUNT(*) c FROM media_item")["c"], 1)
        self.assertEqual(db.q1("SELECT scrape_state FROM media_item")["scrape_state"], "manual")
        self.assertEqual(scanner._item_reg["tv::one"], 2)

    def test_force_scan_skips_deleted_queued_items(self):
        self.item(1, "tv::one")
        self.item(2, "tv::two")
        self.file(1, 1, 1)
        self.file(2, 2, 1)
        with patch.object(tmdb, "api_key", "test"), patch.object(tmdb, "search", return_value={"id": 108978}) as search, patch.object(tmdb, "details", return_value=self.fake_tmdb()), patch.object(tmdb, "download_image", return_value=None), patch.object(tmdb, "season", return_value=None):
            scanner._scrape_items(force=True)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(db.q1("SELECT COUNT(*) c FROM media_item")["c"], 1)
        self.assertEqual(scanner.status.scraped, scanner.status.items_total)

    def test_pending_index_batch_finishes_before_runtime_merge(self):
        self.item(1, "tv::alpha", state="manual")
        self.item(2, "tv::reacher")
        self.file(1, 1, 1, "Alpha.S01E01.mp4")
        self.file(2, 2, 1)
        buffered, release, attempted, merged = (threading.Event() for _ in range(4))

        def walk(_root):
            names = ["Alpha.S01E01.mp4", "Reacher.S02E01.mp4"]
            yield "/shows", [], [ftpclient.FtpEntry(n, "/shows/" + n, False, 100) for n in names]
            buffered.set()
            if not release.wait(5):
                raise RuntimeError("index release timed out")

        def merge():
            attempted.set()
            result = series.consolidate(2)
            merged.set()
            return result

        with patch.dict(os.environ, {"LIBRARY_ROOTS": "/shows|tv"}), patch.object(scanner.ftpclient, "walk", walk), patch.object(scanner, "_wanted", return_value=True):
            with ThreadPoolExecutor(max_workers=2) as pool:
                indexing = pool.submit(scanner._scan_files, True)
                try:
                    self.assertTrue(buffered.wait(5))
                    mutation = pool.submit(merge)
                    self.assertTrue(attempted.wait(5))
                    self.assertFalse(merged.wait(0.1), "merge deleted IDs referenced by an unflushed index batch")
                finally:
                    release.set()
                indexing.result(timeout=5)
                self.assertEqual(mutation.result(timeout=5), 1)
        self.assertEqual(db.q1("SELECT COUNT(*) c FROM media_file WHERE item_id=1")["c"], 2)
        self.assertEqual(db.q1("PRAGMA foreign_key_check"), None)

    def test_alias_does_not_override_another_primary_or_route_to_movie(self):
        self.item(1, "tv::one")
        self.item(2, "tv::two", identity=42)
        self.item(3, "movie::one::", kind="movie")
        db.execute("INSERT INTO media_item_alias(guess_key,item_id) VALUES('tv::two',1)")
        db.execute("INSERT INTO media_item_alias(guess_key,item_id) VALUES('tv::movie-alias',3)")
        scanner._load_registry()
        self.assertEqual(scanner._item_reg["tv::two"], 2)
        self.assertNotIn("tv::movie-alias", scanner._item_reg)

    def test_kind_change_clears_incompatible_id_and_keeps_specials(self):
        self.item(1, "movie::one::", identity=108978, kind="movie")
        self.item(2, "tv::two", identity=108978)
        db.execute("INSERT INTO media_file(item_id,ftp_path,filename) VALUES(1,'/one/Reacher.S00E01.mp4','Reacher.S00E01.mp4')")
        with patch.object(tmdb, "api_key", "test"), patch.object(tmdb, "search", return_value=None):
            response = self.client.patch("/api/items/1", json={"kind": "tv"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(db.q1("SELECT tmdb_id FROM media_item WHERE id=1")["tmdb_id"], None)
        self.assertEqual(db.q1("SELECT COUNT(*) c FROM media_item")["c"], 2)
        self.assertEqual(db.q1("SELECT season FROM episode WHERE item_id=1")["season"], 0)
        self.assertEqual(db.q1("SELECT scrape_state FROM media_item WHERE id=1")["scrape_state"], "manual")

    def test_rekey_conflict_is_explicit_and_does_not_merge(self):
        self.item(1, "movie::one::", kind="movie")
        self.item(2, "tv::one", identity=42)
        response = self.client.patch("/api/items/1", json={"kind": "tv"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(db.q1("SELECT kind FROM media_item WHERE id=1")["kind"], "movie")


if __name__ == "__main__":
    unittest.main(verbosity=2)
