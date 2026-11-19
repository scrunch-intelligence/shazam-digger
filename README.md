# Shazam Digger

You Shazam a lot of songs in clubs, on the radio and in DJ sets. Most of them you never look at again.
Shazam Digger turns that list into a short pile of tracks worth buying.

## What it does

1. **Reads your Shazams.** On Android it reads the list straight off the phone over a USB cable (adb).
   On any other phone you paste a text list.
2. **Keeps only the songs you Shazamed 2 or more times.** One tag is often a mistake. Two or more means you
   really wanted it. You can change this with `min_tags` in `config.toml`.
3. **Builds a Keep / Drop page.** You play each track's 30 second preview and press Keep or Drop.
   The next one plays on its own. Every card shows its release date, with an "older release" chip when it came out
   more than 8 weeks ago. If you already own a track, **More > I have it** files it away for good.
   Every card also gets a YouTube link, but only a checked one: the artist, the title and the mix must match, and the
   video must be playable. If no video passes, there is no link; the tool never guesses. Tracks that have no preview
   and no checked video wait at the end of the page under **Can't play here**, with **Copy all**.
4. **Makes a buy list** of your keeps with search links for your own music store and record pool
   (`store_search` and `pool_search` in `config.toml`).
5. **Learns from your picks.** Artists you kept before move to the top of the next page. **I have it** counts as a
   Keep for learning (you own it, so it fits your taste), but it never goes on a buy list and is never exported as a
   keep. With the optional sound taste it also listens: tracks that sound like your Keeps, and unlike your Drops,
   move up.

Optional extras: it can clear the pulled Shazams off the phone, and it can make a Rekordbox playlist of the
tracks you bought.

## The human-pace rule

This tool works like a person, not like a robot.

- Anything that needs a login, like buying from a store or downloading from your record pool, you do in your own
  browser, where you are already signed in. The tool only gives you the links. It never asks for or keeps passwords.
- It looks things up at a normal speed: one track at a time, a couple of seconds apart. It never scrapes store or pool
  pages. Its own requests (30 second previews and release dates from iTunes and Deezer) say who it is and never pretend
  to be a browser. YouTube lookups go through [yt-dlp](https://github.com/yt-dlp/yt-dlp), an open source program that
  does its own talking to YouTube; the tool only reads search results and video details through it, and downloads nothing.
- If a site shows a CAPTCHA or a "are you a robot?" check, the tool stops and tells you.
  You open the link in your own browser and clear the check yourself.
- It never tries to solve, skip or hide from a CAPTCHA or bot check. Please keep it that way in any change you send.

## Your subscriptions, your terms

The store and record pool links are templates you set yourself (`store_search` and `pool_search` in `config.toml`).
You bring your own store accounts and record pool subscriptions. You are responsible for using them, and every other
site this tool points you at, under that site's own terms. The tool does not log in for you, does not download from
stores or pools, and ships with no store or pool built in.

## Install

Needs Python 3.11 or newer.

Mac / Linux:

    ./setup.sh

Windows (PowerShell):

    py -3.12 -m venv .venv
    .venv\Scripts\python -m pip install -r requirements.txt
    copy config.example.toml config.toml

Settings are in `config.toml`. You can leave it as it is.

## Get your Shazams

**Android over USB:** install `adb` (Mac: `brew install android-platform-tools`), turn on USB debugging on the
phone, plug it in and tap Allow. Keep the phone unlocked on the home screen. The tool opens Shazam's song list and
scrolls it. If you are using another app on the phone, it stops and does not tap over you.

**Any phone:** make a text file with one song per line, `Artist - Title`. A song listed twice counts as two
Shazams. See `sample/shazams.txt`.

## Run

    .venv/bin/python digger.py list sample/shazams.txt     # any phone: your list
    .venv/bin/python digger.py android                     # Android over USB
    .venv/bin/python digger.py soulseek keeps.txt          # find and download your keeps on Soulseek (--look = search only)
    .venv/bin/python digger.py find-tracks keeps.txt       # buy list: out/buy-list.html
    .venv/bin/python digger.py page                        # rebuild the page; looks up any missing checked YouTube links first
    .venv/bin/python digger.py learn picks.csv             # learn your taste from your Keeps, Drops and I have it
    .venv/bin/python digger.py clear-phone                 # Android: clear pulled Shazams (dry run unless --yes)
    .venv/bin/python digger.py rekordbox-xml FOLDER keeps.txt   # Rekordbox playlist of bought keeps

The page is `out/picks.html`. Open it in any browser. Your picks stay in that browser. The small gear at the top has
the short help and the export buttons: **Export keeps** saves `keeps.txt` or `keeps.csv` (Keeps only), and **Export
picks** saves `picks.csv` (Keeps, Drops and I have it, for `learn`). **More** on each track has your store and record
pool links (when set), **I have it** and copy. **Clear heard** drops every track you played but did not keep or own,
after one confirm, with Undo.

Your data stays on your computer in `data/`. Nothing is uploaded.

## How it learns your taste

`learn` reads your own decisions and nothing else: no genre is built in. `taste.py` is a small logistic
regression over words that describe each track (artists and remixers, mix type such as Extended / Original /
Radio, and genre and label if your CSV has those columns). Tracks you kept (or marked I have it) push their words up, tracks you dropped
push them down. Add `--library mytracks.txt` (tracks you already play) and they count as Keeps too, but only
through their artists, remixers and label: a library's folder names and file names are never learned, or the model
would only learn "looks like my library". Run it again
whenever you have new picks. Every Keep, Drop and I have it you ever gave is kept in `data/decisions.json` (newest pick wins), so
it always learns from everything you decided, not only the latest file.

With 5 or more Keeps and Drops it prints an honest check: each song is hidden, the model is trained on the rest and
then guesses it. A score of 0.5 means a coin toss. Expect it to be close to 0.5 until you have many decisions.
Once your decisions span more than one day it also prints a time-split check (train on older decisions, test on
the newest) with a 90% range. The real test is still how often you keep what it suggests.

### Sound taste (optional)

    ./setup.sh --sound

This adds numpy, onnxruntime and one 18 MB model file (Discogs-EffNet, made by the Music Technology Group in
Barcelona, licence CC BY-NC-SA 4.0; it is downloaded from them, not shipped in this repo). You also need ffmpeg.
After that, `learn` turns each 30 s preview into a sound fingerprint. For any track it finds the 3 Keeps and the
3 Drops it sounds most like; closer to your Keeps means higher on the page. It needs 5 Keeps and 5 Drops with a
preview, and it prints an honest check like the one above. Treat it as promising, not proven: with few
decisions it is close to a coin toss. The code is small
(`sound.py`, `sound_fp.py`) so you can improve it.

Ideas for a better taste algorithm (BPM, key, better sound models) are welcome as issues or pull requests. Keep it small, keep
it on the user's own computer, and keep the human-pace rule.

## Soulseek (optional)

Many records, old vinyl-only ones above all, are in no store. `soulseek` hands your list to
[Sockseek](https://github.com/fiso64/sockseek), an open source Soulseek downloader. Download it from its releases
page, put your Soulseek username and password in its `sockseek.conf`, and set `soulseek_bin` in `config.toml`.

    .venv/bin/python digger.py soulseek keeps.txt --look   # only show what is on Soulseek
    .venv/bin/python digger.py soulseek keeps.txt          # download the best copy of each

What you download is your own responsibility. Share back what you can; that is how Soulseek works.

Ideas for a better taste algorithm (BPM, key, better sound models) are welcome as issues or pull requests. Keep it small, keep
it on the user's own computer, and keep the human-pace rule.

## Tests

    .venv/bin/python -m unittest test_digger

The tests include a check of the page's own buttons; it runs when `node` is installed and is skipped otherwise.

## License

MIT. See `LICENSE`.
