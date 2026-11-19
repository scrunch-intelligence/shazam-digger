import datetime as dt, json, os, subprocess, tempfile, unittest
import digger as d
import taste as learned


class Grouping(unittest.TestCase):
    def test_repeats_score_and_one_off_folds_in(self):
        raw = [{"title": "Sample Song (Original Mix)", "artist": "Test Artist", "when": "3 Sept, 20:05"}] * 3 + \
              [{"title": "Sample Song", "artist": "Test Artist.", "when": "3 Sept, 20:05"},
               {"title": "Other Tune", "artist": "Other Act", "when": "9 Oct, 13:40"}]
        out = d.group(raw, dt.date(2026, 10, 14))
        self.assertEqual(out[0]["score"], 4)
        self.assertIn("Sample Song", out[0]["versions"])
        self.assertEqual(out[1]["score"], 1)

    def test_year_rolls_back_for_later_months(self):
        self.assertEqual(d.shazam_time("30 Dec, 10:00", dt.date(2027, 1, 5)).year, 2026)

    def test_merge_keeps_links_and_screenshot_tracks(self):
        old = [{"artist": "A", "title": "T", "youtube": "https://www.youtube.com/watch?v=abcdefghijk", "note": "x"},
               {"artist": "Unreleased / ID", "title": "Show at 1:00", "source": "screenshot"}]
        new = d.merge(old, [{"artist": "A", "title": "T", "score": 3}])
        self.assertEqual(new[0]["youtube"], "https://www.youtube.com/watch?v=abcdefghijk")
        self.assertEqual(new[0]["score"], 3)
        self.assertEqual(new[1]["source"], "screenshot")


class ListInput(unittest.TestCase):
    def test_text_list_counts_repeats(self):
        rows = d.parse_list("# my shazams\nDaft Punk - One More Time\n\n1. Fatboy Slim – Praise You\nDaft Punk - One More Time (Radio Edit)\nno dash here\n")
        self.assertEqual(len(rows), 3)
        tracks = d.count_list(rows)
        self.assertEqual([(t["artist"], t["score"]) for t in tracks], [("Daft Punk", 2), ("Fatboy Slim", 1)])

    def test_csv_with_artist_and_title_columns(self):
        rows = d.parse_list("Index,TagTime,Title,Artist,URL\n1,2026-01-01,Praise You,Fatboy Slim,http://x\n")
        self.assertEqual(rows, [{"artist": "Fatboy Slim", "title": "Praise You"}])


class Phone(unittest.TestCase):
    def test_find_run_stops_at_known_shazams(self):
        rows = [{"title": t, "artist": "x", "when": "1 Oct, 10:00"} for t in "N1 N2 K1 K1 K2".split()]
        sig = [["K1", "x", "1 Oct, 10:00"], ["K1", "x", "1 Oct, 10:00"], ["K2", "x", "1 Oct, 10:00"]]
        self.assertEqual(d.find_run(rows, sig), 2)
        self.assertEqual(d.find_run(rows, [["Z", "x", "1 Oct, 10:00"]]), -1)


class PhoneSafety(unittest.TestCase):
    def setUp(self):
        self.cfg = dict(d.CFG)

    def tearDown(self):
        d.CFG.clear(); d.CFG.update(self.cfg)

    def test_serial_from_environment_when_config_empty(self):
        d.CFG["adb_serial"] = ""
        os.environ["ANDROID_SERIAL"] = "PHONE123"
        try:
            self.assertEqual(d.serial(), "PHONE123")
            d.CFG["adb_serial"] = "CONFIGURED"
            self.assertEqual(d.serial(), "CONFIGURED")
        finally:
            del os.environ["ANDROID_SERIAL"]

    def test_phone_in_use_only_when_another_app_is_open(self):
        def fake(screen_on, act):
            return lambda *a: (f"  mResumedActivity: ActivityRecord{{1 u0 {act} t9}}" if "activities" in a[-1]
                               else f"mWakefulness={'Awake' if screen_on else 'Asleep'}")
        real = d.adb
        try:
            for on, act, busy in [(True, "com.whatsapp/.Main", True), (False, "com.whatsapp/.Main", False),
                                  (True, "com.android.launcher3/.Launcher", False), (True, "com.shazam.android/.Main", False)]:
                d.adb = fake(on, act)
                self.assertEqual(d.phone_in_use(), busy, act)
        finally:
            d.adb = real


class BuyList(unittest.TestCase):
    def test_store_link_from_config(self):
        d.CFG["store_search"] = "https://store.example/search?q={q}"
        try:
            self.assertEqual(d.store_link({"artist": "Modjo", "title": "Lady (Hear Me Tonight)"}), "https://store.example/search?q=Modjo%20Lady")
        finally:
            d.CFG["store_search"] = ""
        self.assertEqual(d.store_link({"artist": "Modjo", "title": "Lady"}), "")

    def test_buy_list_writes_neutral_files_without_fetching(self):
        d.CFG["store_search"] = "https://store.example/search?q={q}"
        try:
            rows = d.buy_rows([{"artist": "Modjo", "title": "Lady"}, {"artist": "Daft Punk", "title": "One More Time"}])
        finally:
            d.CFG["store_search"] = ""
        self.assertEqual(rows[0]["store"], "https://store.example/search?q=Modjo%20Lady")
        self.assertEqual(rows[0]["pool"], "")
        out = tempfile.mkdtemp()
        d.write_buy_list(rows, out)
        self.assertEqual(sorted(os.listdir(out)), ["buy-list.csv", "buy-list.html", "buy-list.txt"])
        txt = open(os.path.join(out, "buy-list.txt"), encoding="utf-8").read().splitlines()
        self.assertEqual(txt[0], "Modjo - Lady\thttps://store.example/search?q=Modjo%20Lady")

    def test_no_store_link_when_unset(self):
        rows = d.buy_rows([{"artist": "A", "title": "B"}])
        out = tempfile.mkdtemp()
        d.write_buy_list(rows, out)
        self.assertNotIn("Store", open(os.path.join(out, "buy-list.html"), encoding="utf-8").read().split("<table>")[1].replace("<th></th>", ""))

    def test_honest_user_agent(self):
        self.assertNotIn("Mozilla", d.UA)

    def test_pool_link_from_config(self):
        d.CFG["pool_search"] = "https://pool.example/search?q={q}"
        try:
            self.assertEqual(d.pool_link({"artist": "Modjo", "title": "Lady (Hear Me Tonight)"}), "https://pool.example/search?q=Modjo%20Lady")
        finally:
            d.CFG["pool_search"] = ""
        self.assertEqual(d.pool_link({"artist": "Modjo", "title": "Lady"}), "")


class Rekordbox(unittest.TestCase):
    def test_matches_files_and_writes_playlist(self):
        import xml.etree.ElementTree as ET
        keeps = [{"artist": "Modjo", "title": "Lady (Hear Me Tonight)"}, {"artist": "Daft Punk", "title": "One More Time"},
                 {"artist": "Fatboy Slim", "title": "Praise You"}]
        files = ["/m/Modjo - Lady (Hear Me Tonight) (Original Mix).mp3", "/m/Daft Punk - One More Time.aiff", "/m/Other - Song.mp3"]
        pairs = d.match_files(keeps, files)
        self.assertEqual([f for _, f in pairs], [files[0], files[1], None])
        root = ET.fromstring(d.rekordbox_xml(pairs))
        self.assertEqual(root.find("COLLECTION").get("Entries"), "2")
        self.assertTrue(root.find("COLLECTION/TRACK").get("Location").startswith("file://localhost/m/Modjo%20-%20Lady"))
        pl = root.find("PLAYLISTS/NODE/NODE")
        self.assertEqual((pl.get("Name"), len(pl)), ("Shazam Keeps", 2))


class Screenshots(unittest.TestCase):
    def test_reads_youtube_player(self):
        lines = ["6:51", "m.youtube.com/watch?v=9SRj", "YouTube", "Open App", "31:37 / 2:00:00", "In this video >",
                 "Example Radio Show 120", "@ExampleShow 12 likes 1.1k views"]
        self.assertEqual(d.read_player(lines), {"pos": 1897, "title": "Example Radio Show 120", "vid": "9SRj"})

    def test_moments_count_every_screenshot_in_a_burst(self):
        s = lambda name, pos=None, title="EXAMPLE 120": {"name": f"Screenshot_20261012-{name}.png",
                                                       "info": {"pos": pos, "title": title, "vid": None} if pos else None}
        ms = d.moments([s("185124", 1897), s("185127"), s("185130"), s("191118", 3025, "Comments 32"), s("191122", 3028)])
        self.assertEqual([(m["shots"], m["positions"][0]) for m in ms], [(3, 1897), (2, 3025)])


class Page(unittest.TestCase):
    def test_only_songs_shazamed_twice_or_more(self):
        out = os.path.join(tempfile.mkdtemp(), "picks.html")
        d.build_page([{"artist": "Once", "title": "One Tag", "score": 1}, {"artist": "Twice", "title": "Two Tags", "score": 2}], out, taste={})
        page = open(out, encoding="utf-8").read()
        self.assertIn("Two Tags", page)
        self.assertNotIn("One Tag", page)

    def test_kept_artists_rise(self):
        taste = d.learn([{"artist": "Modjo", "title": "Lady"}, {"artist": "Modjo & Friend", "title": "Other"}])
        self.assertEqual(taste["artists"]["modjo"], 2)
        out = os.path.join(tempfile.mkdtemp(), "picks.html")
        d.build_page([{"artist": "Daft Punk", "title": "One More Time", "score": 2}, {"artist": "Modjo", "title": "Chillin", "score": 2}],
                     out, taste=taste["artists"])
        page = open(out, encoding="utf-8").read()
        self.assertLess(page.index("Chillin"), page.index("One More Time"))

    def test_page_holds_the_tracks(self):
        out = os.path.join(tempfile.mkdtemp(), "picks.html")
        d.build_page([{"artist": "Fatboy Slim", "title": "Praise You </script>", "score": 2}], out, taste={})
        page = open(out, encoding="utf-8").read()
        self.assertIn("Fatboy Slim", page)
        self.assertNotIn("__DATA__", page)
        self.assertNotIn("Praise You </script>", page)  # data can't break out of the script tag


class LearnedTaste(unittest.TestCase):
    """Made-up tracks only: the learner must follow the user's own Keeps and Drops, with no genre built in."""
    keeps = [{"artist": f"Artist {i}", "title": f"Song {i} (Extended Mix)", "genre": "Style A"} for i in range(8)]
    drops = [{"artist": f"Other {i}", "title": f"Tune {i} (Radio Edit)", "genre": "Style B"} for i in range(8)]

    def test_follows_keeps_and_drops(self):
        m = learned.train(self.keeps, self.drops)
        a = learned.score(m, {"artist": "New", "title": "X (Extended Mix)", "genre": "Style A"})
        b = learned.score(m, {"artist": "New", "title": "Y (Radio Edit)", "genre": "Style B"})
        self.assertGreater(a, 0.5)
        self.assertLess(b, 0.5)
        self.assertTrue(any("style" in k or "mix:" in k for k, _ in learned.strongest(m)["for"]))

    def test_same_data_flipped_flips_the_taste(self):  # nothing is hard-coded: swap the decisions, swap the result
        m = learned.train(self.drops, self.keeps)
        self.assertLess(learned.score(m, {"artist": "New", "title": "X (Extended Mix)", "genre": "Style A"}), 0.5)

    def test_library_counts_as_keeps_through_artists(self):
        lib = [{"artist": "Lib Artist", "title": f"L {i}", "genre": "Style C"} for i in range(20)]
        with_lib = learned.train(self.keeps, self.drops, lib)
        without = learned.train(self.keeps, self.drops)
        t = {"artist": "Lib Artist", "title": "Z", "genre": "Style B"}
        self.assertGreater(learned.score(with_lib, t), learned.score(without, t))

    def test_library_folder_genre_is_never_learned(self):  # a folder name says "library", not taste
        lib = [{"artist": f"Lib {i}", "title": f"L {i}", "genre": "Folder Style"} for i in range(30)]
        m = learned.train(self.keeps, self.drops, lib)
        self.assertNotIn("g:folder", m["weights"])
        a = learned.score(m, {"artist": "New", "title": "Q (Extended Mix)", "genre": "Folder Style"})
        b = learned.score(m, {"artist": "New", "title": "Q (Extended Mix)", "genre": "Unknown Style"})
        self.assertAlmostEqual(a, b, places=6)

    def test_unlabelled_pool_is_a_weak_no(self):
        pool = [{"artist": f"P {i}", "title": f"T {i} (Extended Mix)", "genre": "Style D"} for i in range(40)]
        m = learned.train(self.keeps, self.drops, unlabelled=pool)
        self.assertLess(m["weights"].get("g:d", 0), 0)
        self.assertGreater(learned.score(m, {"artist": "New", "title": "X (Extended Mix)", "genre": "Style A"}), 0.5)

    def test_seeds_and_time_split(self):
        S = learned.seeds(self.keeps, [{"artist": "Lib Artist", "title": "L (Someone Remix)", "label": "Lbl"}], self.drops)
        self.assertIn("someone", S["names"])
        self.assertEqual(learned.seed_match({"artist": "Artist 3", "title": "New", "label": "lbl"}, S), ["artist 3", "lbl"])
        rows = [dict(t, pick="keep", at="2026-01-0%d" % (1 + i % 2)) for i, t in enumerate(self.keeps)] + \
               [dict(t, pick="drop", at="2026-01-0%d" % (1 + i % 2)) for i, t in enumerate(self.drops)]
        r = learned.time_split(rows, "2026-01-02")
        self.assertGreater(r["auc"], 0.9)
        self.assertEqual(len(r["interval_90"]), 2)

    def test_honest_check_hides_each_song(self):
        c = learned.check(self.keeps, self.drops)
        self.assertEqual((c["keeps"], c["drops"]), (8, 8))
        self.assertGreater(c["auc"], 0.9)  # a clean made-up split is easy; real taste is much harder

    def test_picks_csv_and_page_order(self):
        rows = d.parse_list("Artist,Title,Pick\nA1,Song (Extended Mix),keep\nB1,Tune (Radio Edit),drop\n")
        self.assertEqual([r["pick"] for r in rows], ["keep", "drop"])
        m = learned.train(self.keeps, self.drops)
        out = os.path.join(tempfile.mkdtemp(), "picks.html")
        d.build_page([{"artist": "Zed", "title": "Radio One (Radio Edit)", "score": 2},
                      {"artist": "Zed", "title": "Long One (Extended Mix)", "score": 2}], out, taste={"artists": {}, "model": m})
        page = open(out, encoding="utf-8").read()
        self.assertLess(page.index("Long One"), page.index("Radio One"))


    def test_learning_keeps_every_earlier_decision(self):
        store = os.path.join(tempfile.mkdtemp(), "decisions.json")
        first = [dict(t, pick="keep") for t in self.keeps[:4]] + [dict(t, pick="drop") for t in self.drops[:4]]
        second = [dict(t, pick="keep") for t in self.keeps[4:]] + [dict(t, pick="drop") for t in self.drops[4:]]
        d.remember_decisions(first, store)
        k, dr = d.remember_decisions(second, store)
        self.assertEqual((len(k), len(dr)), (8, 8))  # both files count, not only the newest
        k2, dr2 = d.remember_decisions([dict(self.keeps[0], pick="drop")], store)  # a changed mind: newest pick wins
        self.assertEqual((len(k2), len(dr2)), (7, 9))
        both = learned.check(k, dr)["auc"]
        alone = learned.check([t for t in second if t["pick"] == "keep"], [t for t in second if t["pick"] == "drop"])["auc"]
        self.assertGreaterEqual(both, alone)


class SoundTaste(unittest.TestCase):
    def test_sound_model_prefers_what_sounds_like_keeps(self):
        try:
            import numpy as np
        except ImportError:
            self.skipTest("numpy not installed (sound taste is optional)")
        import sound
        rng = np.random.default_rng(0)
        a, b = rng.normal(size=32), rng.normal(size=32)
        keeps = [a + 0.1 * rng.normal(size=32) for _ in range(6)]
        drops = [b + 0.1 * rng.normal(size=32) for _ in range(6)]
        m = sound.build(keeps, drops)
        self.assertGreater(m.score(a), m.score(b))
        self.assertIsNone(sound.build(keeps[:2], drops))
        self.assertGreater(sound.check(keeps, drops), 0.9)

    def test_soulseek_hands_the_list_to_sockseek(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = os.path.join(tmp, "sockseek"); open(exe, "w").close()
            keeps = os.path.join(tmp, "keeps.txt"); open(keeps, "w").write("Modjo - Lady\n")
            old = dict(d.CFG); d.CFG.update(soulseek_bin=exe, soulseek_dir=os.path.join(tmp, "dl"), soulseek_conf="")
            try:
                calls = []
                d.cmd_soulseek(keeps, look=True, run=calls.append)
            finally:
                d.CFG.clear(); d.CFG.update(old)
            self.assertEqual(calls[0][0], exe)
            self.assertIn("--print", calls[0])
            self.assertIn("Modjo,Lady", open(calls[0][1]).read())


class YouTubeLinks(unittest.TestCase):
    """A link is kept only when it is the same release. No network: yt-dlp answers are stood in for."""
    modjo = {"artist": "Modjo", "title": "Lady (Hear Me Tonight)"}

    def ok(self, t, **info):
        return d.yt_judge(t, dict({"channel": "Someone", "duration": 300}, **info))

    def test_same_release_is_accepted(self):
        self.assertTrue(self.ok(self.modjo, title="Modjo - Lady (Hear Me Tonight) (Official Video)"))
        self.assertTrue(self.ok(self.modjo, title="Lady (Hear Me Tonight)", channel="Modjo - Topic"))  # artist on the channel
        self.assertTrue(self.ok({"artist": "Beyonc\u00e9", "title": "Halo"}, title="Beyonce - Halo"))
        self.assertTrue(self.ok({"artist": "Modjo", "title": "Lady (Original Mix)"}, title="Modjo - Lady"))

    def test_wrong_artist_title_or_version_is_refused(self):
        self.assertFalse(self.ok(self.modjo, title="Lady - Someone Else", channel="Other"))
        self.assertFalse(self.ok(self.modjo, title="Modjo - Chillin"))
        for word in ("(Cover)", "(Live at a festival)", "(Slowed + Reverb)", "(Karaoke)", "(Instrumental)"):
            self.assertFalse(self.ok(self.modjo, title="Modjo - Lady " + word), word)
        self.assertFalse(self.ok({"artist": "\u041a\u0438\u043d\u043e", "title": "\u0413\u0440\u0443\u043f\u043f\u0430"}, title="\u041a\u0438\u043d\u043e - \u0413\u0440\u0443\u043f\u043f\u0430"))  # cannot be checked: no guess

    def test_the_mix_must_match(self):
        self.assertFalse(self.ok(self.modjo, title="Modjo - Lady (Someone Remix)"))  # we want the original
        self.assertFalse(self.ok(self.modjo, title="Modjo - Lady (Dub Mix)"))
        remix = {"artist": "Modjo", "title": "Lady (Someone Remix)"}
        self.assertTrue(self.ok(remix, title="Modjo - Lady (Someone Extended Remix)"))
        self.assertTrue(self.ok(remix, title="Modjo - Lady - Someone Remix"))
        self.assertFalse(self.ok(remix, title="Modjo - Lady (Other Remix)"))
        self.assertFalse(self.ok(remix, title="Modjo - Lady"))
        self.assertTrue(self.ok({"artist": "Modjo", "title": "Lady (Extended Mix)"}, title="Modjo - Lady (Radio Edit)"))

    def test_it_must_be_playable(self):
        t = "Modjo - Lady"
        self.assertFalse(self.ok(self.modjo, title=t, duration=20))      # a snippet
        self.assertFalse(self.ok(self.modjo, title=t, duration=3 * 3600))  # a whole set
        self.assertFalse(self.ok(self.modjo, title="Modjo - Lady (DJ Set at a club)"))
        self.assertFalse(self.ok(self.modjo, title="Modjo - Lady (Mashup)"))
        self.assertFalse(self.ok(self.modjo, title=t, playable_in_embed=False))
        self.assertFalse(self.ok(self.modjo, title=t, availability="private"))
        self.assertFalse(self.ok(self.modjo, title=t, live_status="is_live"))
        self.assertFalse(self.ok(self.modjo, title=t, age_limit=18))

    def lookup_with(self, results, infos, check):
        old = (d.yt_search, d.yt_info, d.CFG["youtube_check"], d.LOOKUP_PAUSE)
        d.yt_search, d.yt_info, d.CFG["youtube_check"], d.LOOKUP_PAUSE = (lambda q, n=5: results), (lambda v: infos.get(v)), check, 0
        try:
            return d.youtube_lookup(self.modjo)
        finally:
            d.yt_search, d.yt_info, d.CFG["youtube_check"], d.LOOKUP_PAUSE = old

    def test_lookup_takes_the_first_video_that_passes_both_checks(self):
        hit = lambda i, title: {"id": i * 11, "title": title, "channel": "x", "duration": 280}
        results = [hit("a", "Modjo - Lady (Someone Remix)"), hit("b", "Modjo - Lady"), hit("c", "Modjo - Lady (Hear Me Tonight)")]
        infos = {"b" * 11: dict(results[1], playable_in_embed=False), "c" * 11: dict(results[2], upload_date="20001101")}
        self.assertEqual(self.lookup_with(results, infos, True)["id"], "c" * 11)  # b is blocked from playing
        self.assertIsNone(self.lookup_with(results, {}, True))                  # the full check found nothing playable: no link
        self.assertEqual(self.lookup_with(results, {}, False)["id"], "b" * 11)  # title check only (youtube_check = false), still not a guess

    def test_a_robot_check_stops_everything(self):
        class R:
            stdout, stderr, returncode = "", "ERROR: Sign in to confirm you\u2019re not a bot", 1
        real = d.subprocess.run
        d.subprocess.run = lambda *a, **k: R()
        try:
            with self.assertRaises(d.BotCheck):
                d.ytdlp("--dump-json", "x")
        finally:
            d.subprocess.run = real
        tracks = [{"artist": "A", "title": "One", "score": 2}, {"artist": "B", "title": "Two", "score": 2}]
        def refuse(t):
            raise d.BotCheck("stop")
        old, d.add_youtube, d.LOOKUP_PAUSE = d.add_youtube, refuse, 0
        try:
            d.fill_in(tracks)
        finally:
            d.add_youtube = old
        self.assertFalse(any(t.get("checked") for t in tracks))  # nothing after the check was touched or marked

    def test_old_unchecked_link_is_dropped_when_it_is_another_release(self):
        t = {"artist": "Modjo", "title": "Lady (Hear Me Tonight)", "youtube": "https://www.youtube.com/watch?v=" + "x" * 11, "yt_year": "2011"}
        real = (d.yt_info, d.youtube_lookup, d.CFG["youtube_check"])
        d.yt_info, d.youtube_lookup, d.CFG["youtube_check"] = (lambda v: {"id": v, "title": "Someone else - Other song", "channel": "q"}), (lambda t: None), True
        try:
            d.add_youtube(t)
        finally:
            d.yt_info, d.youtube_lookup, d.CFG["youtube_check"] = real
        self.assertNotIn("youtube", t)
        self.assertIn("yt_none", t)


class ReleaseDateAndCantPlay(unittest.TestCase):
    def test_release_date_comes_with_the_preview_match(self):
        t = {"artist": "Modjo", "title": "Lady (Hear Me Tonight)"}
        real = d.get
        d.get = lambda url: {"results": [{"artistName": "Modjo", "trackName": "Lady (Hear Me Tonight)", "previewUrl": "https://p/1.m4a",
                                          "releaseDate": "2000-08-01T07:00:00Z"}]}
        try:
            self.assertEqual(d.find_preview_info(t), ("https://p/1.m4a", "2000-08-01"))
            self.assertEqual(d.find_preview(t), "https://p/1.m4a")
        finally:
            d.get = real

    def test_best_release_date(self):
        self.assertEqual(d.release_of({"released": "2000-08-01", "yt_released": "2001-01-01"}), "2000-08-01")
        self.assertEqual(d.release_of({"yt_released": "2001-01-01"}), "2001-01-01")
        self.assertEqual(d.release_of({"yt_year": "2011"}), "")  # a year alone is shown as 'from video', never as a date

    def test_tracks_that_cannot_play_go_to_the_end_list_but_unchecked_ones_stay(self):
        out = os.path.join(tempfile.mkdtemp(), "picks.html")
        d.build_page([{"artist": "A", "title": "Plays", "score": 2, "preview": "https://p/a.m4a", "checked": True},
                      {"artist": "B", "title": "Nothing", "score": 2, "checked": True},
                      {"artist": "C", "title": "Not looked up", "score": 2},
                      {"artist": "D", "title": "Video only", "score": 2, "youtube": "https://www.youtube.com/watch?v=abcdefghijk", "checked": True}], out, taste={})
        page = open(out, encoding="utf-8").read()
        data = json.loads(page.split("const T=")[1].split(";\nconst CANT")[0])
        cant = json.loads(page.split("const CANT=")[1].split(";\n")[0])
        self.assertEqual(sorted(r["title"] for r in data), ["Not looked up", "Plays", "Video only"])
        self.assertEqual([r["title"] for r in cant], ["Nothing"])
        self.assertEqual(next(r for r in data if r["title"] == "Plays")["yt"], "")  # no link is invented
        self.assertNotIn("__CANT__", page)


class IHaveIt(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.keep = (d.DECISIONS, d.TASTE, d.TRACKS, dict(d.CFG))
        d.DECISIONS, d.TASTE, d.TRACKS = (os.path.join(self.tmp, f) for f in ("decisions.json", "taste.json", "tracks.json"))
        d.CFG["out_dir"] = os.path.join(self.tmp, "out")
        d.CFG["store_search"] = d.CFG["pool_search"] = ""

    def tearDown(self):
        d.DECISIONS, d.TASTE, d.TRACKS = self.keep[:3]
        d.CFG.clear(); d.CFG.update(self.keep[3])

    def test_have_is_saved_as_have_and_taught_as_a_keep(self):
        keeps, drops = d.remember_decisions([{"artist": "A", "title": "One", "pick": "have"}, {"artist": "B", "title": "Two", "pick": "keep"},
                                             {"artist": "C", "title": "Three", "pick": "drop"}])
        self.assertEqual([(r["title"], r["pick"], r["have"]) for r in keeps], [("One", "keep", True), ("Two", "keep", False)])
        self.assertEqual([r["title"] for r in drops], ["Three"])
        self.assertEqual({r["title"]: r["pick"] for r in json.load(open(d.DECISIONS))}, {"One": "have", "Two": "keep", "Three": "drop"})
        keeps, _ = d.remember_decisions([{"artist": "A", "title": "One", "pick": "drop"}])  # a changed mind: newest wins
        self.assertEqual([r["title"] for r in keeps], ["Two"])

    def test_learn_counts_have_like_a_keep(self):
        rows = [{"artist": f"Own {i}", "title": f"Song {i} (Extended Mix)", "pick": "have"} for i in range(6)] + \
               [{"artist": f"Bin {i}", "title": f"Tune {i} (Radio Edit)", "pick": "drop"} for i in range(6)]
        csv_path = os.path.join(self.tmp, "picks.csv")
        open(csv_path, "w").write("Artist,Title,Pick\n" + "\n".join(f'{r["artist"]},{r["title"]},{r["pick"]}' for r in rows) + "\n")
        d.cmd_learn(csv_path)
        taste = json.load(open(d.TASTE))
        self.assertEqual(taste["model"]["trained_on"]["keeps"], 6)
        self.assertGreater(taste["artists"]["own0"], 0)
        m = taste["model"]
        self.assertGreater(learned.score(m, {"artist": "New", "title": "X (Extended Mix)"}), learned.score(m, {"artist": "New", "title": "Y (Radio Edit)"}))

    def test_have_never_reaches_a_buy_list_or_a_playlist(self):
        rows = d.only_keeps(d.parse_list("Artist,Title,Pick\nA,One,keep\nB,Two,have\nC,Three,drop\nD,Four,\n"))
        self.assertEqual([r["title"] for r in rows], ["One", "Four"])
        p = os.path.join(self.tmp, "picks.csv"); open(p, "w").write("Artist,Title,Pick\nA,One,keep\nB,Two,have\n")
        d.cmd_find_tracks(p)
        txt = open(os.path.join(d.CFG["out_dir"], "buy-list.txt"), encoding="utf-8").read()
        self.assertIn("A - One", txt)
        self.assertNotIn("Two", txt)

    def test_have_tracks_are_not_looked_up_or_listed_again(self):
        d.save(d.DECISIONS, [{"artist": "A", "title": "One", "pick": "have"}])
        self.assertEqual(d.owned_keys(), {"a|one"})
        out = os.path.join(self.tmp, "picks.html")
        d.build_page([{"artist": "A", "title": "One", "score": 3, "preview": "https://p/a.m4a"}, {"artist": "B", "title": "Two", "score": 2, "preview": "https://p/b.m4a"}], out, taste={})
        page = open(out, encoding="utf-8").read()
        self.assertNotIn('"One"', page)
        self.assertIn('"Two"', page)
        calls = []
        real, d.add_youtube, d.LOOKUP_PAUSE = d.add_youtube, lambda t: calls.append(t["title"]), 0
        try:
            d.fill_in([{"artist": "A", "title": "One", "score": 3, "preview": "x", "released_checked": True}])
        finally:
            d.add_youtube = real
        self.assertEqual(calls, [])


class EndOfList(unittest.TestCase):
    def test_a_list_that_stops_moving_ends_even_with_repeated_rows(self):
        screens = []
        def screen():
            screens.append(1)
            if len(screens) > 30:
                raise AssertionError("never stopped")
            return [{"title": "Same", "artist": "A", "when": "1 Oct, 10:00", "y": y, "x": 0, "cy": y} for y in (300, 600, 900)]
        real = (d.adb, d.screen_rows, d.screen_size, d.time.sleep)
        d.adb, d.screen_rows, d.screen_size, d.time.sleep = (lambda *a: ""), screen, (lambda: (1080, 2400)), (lambda s: None)
        try:
            got = d.read_new_shazams([])
        finally:
            d.adb, d.screen_rows, d.screen_size, d.time.sleep = real
        self.assertEqual(len(got), 3)  # the three rows once, not again after every swipe


class PageScript(unittest.TestCase):
    def test_buttons_do_what_they_say(self):
        import shutil as sh_
        node = sh_.which("node")
        if not node:
            self.skipTest("node not installed (only the page script check needs it)")
        out = os.path.join(tempfile.mkdtemp(), "picks.html")
        d.build_page([{"artist": "Modjo", "title": "Lady (Hear Me Tonight)", "score": 3, "preview": "https://p/a.m4a", "released": "2000-08-01", "checked": True},
                      {"artist": "Fatboy Slim", "title": "Praise You", "score": 2, "checked": True},
                      {"artist": "Sample Group", "title": "Another Song", "score": 2, "preview": "https://p/b.m4a", "released": dt.date.today().isoformat(), "checked": True},
                      {"artist": "Offline", "title": "Not looked up", "score": 2}], out, taste={})
        r = subprocess.run([node, os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_page.js"), out], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr + r.stdout)


if __name__ == "__main__":
    unittest.main()
