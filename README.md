# Sourcerers

**Find the original video behind a clip — and stop using the same footage twice.**

Sourcerers is a small app you run on your own PC. Drop in a clip or a screenshot from a
ranking / commentary style video and it:

1. samples frames and builds a **visual fingerprint** of the clip,
2. checks your library to tell you **whether you already have that footage**,
3. reverse-searches the frames with **Google Lens + Yandex** (and optionally Bing),
4. hands you a ranked list of **links that could be the original** — with the real
   title and channel attached, so you can judge in two seconds,
5. remembers the source you confirm, how many times you used the clip, and which
   video it went into.

Everything runs locally. Your clips never leave your machine except as the frame you
explicitly search, sent to the search engine you picked.

---

## Quick start

You need **Python 3.10+** and about 2 minutes.

### Windows

Double-click **`run.bat`** (or run it in a terminal). It creates a virtual
environment, installs the dependencies and opens the app.

### macOS / Linux

```bash
./run.sh
```

### Manual

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

The app opens at **http://127.0.0.1:8765**.

`ffmpeg` is bundled through the `imageio-ffmpeg` pip package, so video support works
without installing anything else. If you already have a system ffmpeg, it is used first.

---

## How to use it

### Find sources

- **Drag a clip / screenshot onto the dropzone**, or press `Ctrl`+`V` anywhere to paste
  a screenshot straight from your clipboard. This is the fastest path for
  ranking/commentary work: screenshot the frame → paste → results.
- Or paste a **YouTube / TikTok / Instagram link** and hit *Fetch clip*. Sourcerers
  downloads the first 5 minutes with `yt-dlp`, samples it, and searches it.
- Drop **several files at once** and it works through them in order.

You then get:

- **Pipeline status** — sampling frames → fingerprinting → checking library → searching.
- A **repeat warning** if the footage is already in your library (see below).
- **Ranked links**: each card shows the score, the real title/channel, which engine
  found it, and *why* it is ranked there ("full-length YouTube upload", "seen by
  Google Lens + Yandex", "stock-footage library", "uploaded 4 years ago"…).
- **"This is it"** pins a link as the confirmed original — that is what gets exported
  and shown in your library.

If the engines return nothing, the result page keeps a **browser fallback**: open
Google Lens / Yandex / TinEye and drag in the frame (there is a download button for
exactly that frame), then paste the link you find into *"Add the original by hand"*.

### Repeats (the anti-reuse part)

Every clip gets a fingerprint made of several sampled frames. Uploads are compared
against the whole library *before* anything is searched:

| Verdict | Meaning |
| --- | --- |
| **repeat** | the same footage — different resolution, re-encode or trim doesn't matter |
| **similar** | some frames overlap; worth a quick look |
| **new** | no overlapping footage found |

When it's a repeat, Sourcerers shows you the existing clip (with how many times you
used it and in which video), skips the search, and offers **Search anyway** or
**Discard this copy**.

The **Repeats** tab groups your library by similarity, so you can see at a glance
that clips #4, #11 and #19 are all the same footage.

### Scan folder

Point it at your b-roll folder and it fingerprints every video/image in there
(~1 second per clip, 400 per batch), flagging the ones you already have. Run it
before a batch edit and you won't accidentally reuse something for the third time.

### Library

Everything you've sourced. Search across titles, notes, projects and tags; mark clips
as used; edit notes/project; export everything to CSV (`Library → Export CSV`) as a
source sheet for your edit or your VA.

---

## The engines

| Engine | Key | Notes |
| --- | --- | --- |
| **Google Lens** | none | Best for finding the actual upload. Uses the same endpoint the Lens web page does. |
| **Yandex Images** | none | Often better at mirrors, re-uploads and TikTok/IG copies. |
| **Bing Visual Search** | free Azure key | Optional extra opinion; page links + publish dates. |
| Demo data | none | Fake rows so you can see the layout. Clearly labelled, off by default. |

Both free engines are **unofficial**: they are the same requests a browser makes, not a
public API. That means they can rate-limit you or change shape. Sourcerers handles
that the honest way: it tells you which engine failed and why, and always leaves you
the manual browser path. There is no fake "everything is fine" state.

Tips if Lens starts refusing you:

- space out searches (a handful per minute is fine; 50 in a row is not),
- use the *Thorough search* toggle to send 3 frames only when a fast search misses,
- if you're on a VPN/proxy, the exit node may be flagged — switch it off or use another.

**Bing (optional):** create a free *Bing Search → Visual Search* resource in the Azure
portal, copy key 1, paste it in Settings. It is then available as a search engine.

---

## How the duplicate detection works

Each sampled frame is reduced to three things:

- a **dHash** (64-bit, neighbouring-pixel differences — robust to scaling and colour),
- a **pHash** (64-bit, low-frequency DCT — robust to re-encoding and slight crops),
- a small **RGB histogram** (tie-breaker only, so colour grading doesn't cause false hits).

Comparing two clips is a *bag-of-frames* match (not a sequence match), because reused
footage is usually trimmed or reordered. Every frame of the shorter clip looks for its
best partner in the other clip; how many find one is the **coverage**, how close those
partners are is the **distance**.

```
score = 0.65 × coverage + 0.35 × closeness
verdict = repeat  if coverage ≥ 45% and distance ≤ 0.20
          similar otherwise, when anything overlaps
```

A 3-second excerpt of an 8-second clip still matches (80% coverage on the short side),
and a 480×270 re-encode of a 640×360 file matches at ~98%.

Both thresholds live in **Settings** and apply immediately. Lower the distance, or
raise the coverage, to make matching stricter.

---

## What "the original" means here

No reverse-image service can *prove* provenance, so Sourcerers never claims it.

It ranks candidates by the evidence it actually has: how many independent engines saw
the link, how often it appears on the results page, the type of page (full-length
YouTube upload or stock library ranks above a Short/reel), whether the title reads like
an edit/compilation, an available publish date, and whether the page is still alive
(oEmbed/title enrichment). Each card shows those reasons, and **you** confirm the
original with one click. That confirmed link is what the library, the CSV export and
the "source saved" badge use.

---

## Where your data lives

```
data/
  sourcerers.db        # library, sources, usage counts, settings
  clips/<id>/frames/   # sampled frames (these are what get searched)
  clips/<id>/thumb.jpg # library preview
```

Nothing is uploaded anywhere except the frame(s) you search. Delete the `data/`
folder to start over. Point it somewhere else with the `SOURCERERS_DATA` environment
variable.

---

## Troubleshooting

| Problem | Fix |
| --- | --- |
| "ffmpeg not found" | `pip install imageio-ffmpeg`, or install ffmpeg system-wide |
| Link fetching says yt-dlp is missing | `pip install yt-dlp` (already in `requirements.txt`) |
| Both engines fail with timeouts | no internet, or a firewall/proxy is blocking them; the browser fallback always works |
| Lens says "rate-limited" | wait a minute, or search fewer frames (turn off *Thorough search*) |
| A clip is flagged as a repeat but isn't | Settings → raise *Frame distance* strictness (lower the number) |
| Two clearly different clips match | Settings → lower the distance threshold to ~0.14 |
| Port already in use | `python run.py --port 9000` |
| Wrong thumbnails / weird titles | Settings → turn off *Fetch real titles* |

---

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest            # 37 tests: hashing, dedup, URL parsing, ranking, API flow
python run.py --reload      # auto-reload while editing
```

Layout:

```
app/
  main.py          FastAPI routes, upload jobs, folder scan
  db.py            SQLite storage (clips, candidates, runs, settings)
  hashing.py       dHash / pHash / histogram fingerprints
  dedup.py         clip comparison + verdicts
  media.py         ffmpeg frame sampling, yt-dlp downloads
  search/
    base.py        provider contract, URL canonicalisation, HTML link extraction
    lens.py        Google Lens upload + results parsing
    yandex.py      Yandex CBIR upload + results parsing
    bing.py        Bing Visual Search API (optional key)
    enrich.py      oEmbed / og:title metadata
    rank.py        "how likely is this the original" scoring
  static/          the whole UI (no build step)
tests/
```

Adding an engine is one file: subclass `Provider`, implement `async search(client,
image, meta) -> ProviderOutput`, and register it in `app/search/__init__.py`.
