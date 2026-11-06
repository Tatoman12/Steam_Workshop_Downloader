"""Tests básicos del descargador (sin red ni Steam).

Ejecutar:  python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app  # noqa: E402


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    return _varint((number << 3) | 2) + _varint(len(payload)) + payload


class ParseRefTests(unittest.TestCase):
    def test_urls(self):
        self.assertEqual(
            app.parse_ref("https://steamcommunity.com/sharedfiles/filedetails/?id=836319872"),
            "836319872",
        )
        self.assertEqual(
            app.parse_ref("https://steamcommunity.com/sharedfiles/filedetails/836319872"),
            "836319872",
        )
        self.assertEqual(app.parse_ref(" 836319872 "), "836319872")

    def test_invalid(self):
        self.assertIsNone(app.parse_ref("https://steamcommunity.com/app/250900/workshop/"))
        self.assertIsNone(app.parse_ref("hola"))
        self.assertIsNone(app.parse_ref(""))


class SafeNameTests(unittest.TestCase):
    def test_invalid_chars(self):
        name = app.safe_name('a/b\\c:d*e?f"g<h>i|j')
        for char in '<>:"/\\|?*':
            self.assertNotIn(char, name)

    def test_reserved_and_empty(self):
        self.assertEqual(app.safe_name("CON"), "mod")
        self.assertEqual(app.safe_name("   "), "mod")
        self.assertEqual(app.safe_name("..."), "mod")

    def test_length(self):
        self.assertLessEqual(len(app.safe_name("x" * 300)), 80)


class FormatTests(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(app.human_size(0), "0 B")
        self.assertEqual(app.human_size(1024), "1.0 KB")
        self.assertEqual(app.human_size(1024 * 1024), "1.0 MB")

    def test_human_count(self):
        self.assertEqual(app.human_count(999), "999")
        self.assertEqual(app.human_count(1500), "1,5 K")
        self.assertEqual(app.human_count(2_500_000), "2,5 M")

    def test_int(self):
        self.assertEqual(app._int("12"), 12)
        self.assertEqual(app._int(None), 0)
        self.assertEqual(app._int("x", 7), 7)


class WorkshopParsingTests(unittest.TestCase):
    def test_parse_ssr(self):
        payload = {
            "route": "/workshop/browse",
            "loaderData": [
                json.dumps({"app": {"appid": 250900, "name": "Juego de prueba"}}),
                json.dumps(
                    {
                        "results": [{"publishedfileid": "1", "title": "Mod"}],
                        "total_count": 1,
                        "total_pages": 1,
                        "current_page": 1,
                    }
                ),
            ],
        }
        html = (
            '<script type="application/json" id="valve-ssr-data">'
            + json.dumps(payload)
            + "</script>"
        )
        listing, game = app._parse_ssr(html)
        self.assertEqual(listing["total_count"], 1)
        self.assertEqual(game["name"], "Juego de prueba")

    def test_parse_ssr_without_data(self):
        self.assertEqual(app._parse_ssr("<html></html>"), (None, None))

    def test_workshop_item(self):
        item = app._workshop_item(
            {
                "publishedfileid": "42",
                "consumer_appid": 250900,
                "title": "  Un mod  ",
                "short_description": "Descripción",
                "preview_url": "https://example.com/a.jpg",
                "file_size": "2048",
                "subscriptions": 1500,
                "favorited": 20,
                "star_rating": 4,
                "total_votes": 99,
                "tags": [{"display_name": "Lua"}, {"tag": "Items"}, "ignorar"],
            }
        )
        self.assertEqual(item["fileid"], "42")
        self.assertEqual(item["title"], "Un mod")
        self.assertEqual(item["subscriptions_text"], "1,5 K")
        self.assertEqual(item["size_text"], "2.0 KB")
        self.assertEqual(item["tags"], ["Lua", "Items"])
        self.assertEqual(item["stars"], 4)


class BrowseSortTests(unittest.TestCase):
    def test_recent_sort(self):
        self.assertEqual(app.BROWSE_SORTS["recent"], "mostrecent")
        self.assertEqual(app._sort_name("mostrecent"), "recent")
        self.assertEqual(app._empty_browse("250900", "mostrecent", 1)["sort"], "recent")

    def test_unknown_sort_falls_back_to_popular(self):
        self.assertEqual(app._sort_name("desconocido"), "popular")


class HistoryTests(unittest.TestCase):
    def test_legacy_history_migrates_recent(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "recent": [
                            {
                                "title": "Mod antiguo",
                                "folder": "C:\\mods\\Mod antiguo",
                                "when": 10,
                            }
                        ]
                    }
                ),
                "utf-8",
            )
            old_path = app.CONFIG_PATH
            app.CONFIG_PATH = path
            try:
                cfg = app.load_config()
            finally:
                app.CONFIG_PATH = old_path

        self.assertEqual(cfg["history"][0]["title"], "Mod antiguo")
        self.assertEqual(cfg["history"][0]["game"], "Descargas anteriores")
        self.assertTrue(cfg["history"][0]["legacy"])

    def test_explicit_empty_history_does_not_rebuild_legacy(self):
        cfg = {
            "history": [],
            "recent": [{"title": "Viejo", "folder": "C:\\mods\\Viejo", "when": 10}],
        }
        self.assertEqual(app.load_history(cfg), [])

    def test_history_groups_by_game_and_sorts_by_latest(self):
        entries = [
            {
                "fileid": "1",
                "appid": "250900",
                "game": "The Binding of Isaac",
                "title": "Primero",
                "folder": "C:\\mods\\Primero",
                "when": 100,
            },
            {
                "fileid": "2",
                "appid": "250900",
                "game": "The Binding of Isaac",
                "title": "Segundo",
                "folder": "C:\\mods\\Segundo",
                "when": 300,
            },
            {
                "fileid": "",
                "appid": "",
                "game": "Descargas anteriores",
                "title": "Antiguo",
                "folder": "C:\\mods\\Antiguo",
                "when": 200,
                "legacy": True,
            },
        ]
        groups = app.history_groups(entries)
        self.assertEqual([g["game"] for g in groups], ["The Binding of Isaac", "Descargas anteriores"])
        self.assertEqual(groups[0]["count"], 2)
        self.assertEqual(groups[0]["items"][0]["title"], "Segundo")
        self.assertEqual(groups[1]["count"], 1)

    def test_record_history_uses_game_name_and_updates_duplicates(self):
        cfg = {"history": [], "recent": []}
        job = app.Job(["10", "11"], "C:\\mods", "title")
        job.items = [
            {
                "fileid": "10",
                "appid": 250900,
                "title": "Mod A",
                "size": 2048,
                "size_text": "2.0 KB",
                "thumb": "https://example.com/a.jpg",
            },
            {
                "fileid": "11",
                "appid": 250900,
                "title": "Mod B",
                "size": 0,
                "size_text": "—",
                "thumb": "",
            },
        ]
        job.folders = ["C:\\mods\\Mod A", "C:\\mods\\Mod B"]

        with mock.patch.object(app, "fetch_app_name", return_value="Juego de prueba") as fetch:
            app.record_history(job, cfg)

        self.assertEqual(len(cfg["history"]), 2)
        self.assertEqual(fetch.call_count, 1)
        groups = app.history_groups(cfg["history"])
        self.assertEqual(groups[0]["game"], "Juego de prueba")
        self.assertEqual(groups[0]["count"], 2)

        repeat = app.Job(["10"], "C:\\mods", "title")
        repeat.items = [dict(job.items[0])]
        repeat.folders = ["C:\\mods\\Mod A"]
        with mock.patch.object(app, "fetch_app_name", return_value="Juego de prueba"):
            app.record_history(repeat, cfg)

        self.assertEqual(len(cfg["history"]), 2)
        self.assertEqual(len([e for e in cfg["history"] if e["fileid"] == "10"]), 1)


class LoginTokenTests(unittest.TestCase):
    def test_login_tokens(self):
        entry = _field(1, b"usuario") + _field(2, b"token-secreto")
        raw = _field(4, entry)
        compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
        data = compressor.compress(raw) + compressor.flush()
        self.assertEqual(app._login_tokens(data), {"usuario": "token-secreto"})

    def test_empty(self):
        self.assertEqual(app._login_tokens(b""), {})


class PromoteTests(unittest.TestCase):
    def test_promote_replaces_old_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / ".mod.part-abc"
            target = root / "mod"
            staging.mkdir()
            (staging / "nuevo.txt").write_text("nuevo", "utf-8")
            target.mkdir()
            (target / "viejo.txt").write_text("viejo", "utf-8")

            app._promote(staging, target)

            self.assertTrue((target / "nuevo.txt").exists())
            self.assertFalse((target / "viejo.txt").exists())
            self.assertFalse(staging.exists())
            self.assertEqual(
                [p.name for p in root.iterdir() if p.name.startswith(".mod.old")],
                [],
            )

    def test_promote_new_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            staging = root / ".mod.part-abc"
            target = root / "mod"
            staging.mkdir()
            (staging / "a.txt").write_text("a", "utf-8")

            app._promote(staging, target)

            self.assertTrue((target / "a.txt").exists())
            self.assertFalse(staging.exists())


if __name__ == "__main__":
    unittest.main()
