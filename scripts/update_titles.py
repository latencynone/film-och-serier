#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Uppdaterar titles.json med nya filmer/serier som:
  - finns på Netflix, Apple TV+, HBO Max, Prime Video eller Disney+ i Sverige
  - hade premiär de senaste tio åren
  - har IMDb-betyg 7.0 eller högre

Körs automatiskt en gång i veckan av .github/workflows/update-titles.yml,
men kan också köras manuellt lokalt eller via "Run workflow" på GitHub.

Datakällor (båda gratis):
  - TMDb (themoviedb.org)  -> vad som finns var, releasedatum, genre, handling
  - OMDb (omdbapi.com)     -> IMDb/Rotten Tomatoes/Metacritic-betyg + IMDb-ID

Begränsning värd att känna till: OMDb ger betyg men inga direkta sid-adresser
till Rotten Tomatoes/Metacritic. Nya titlar som hittas automatiskt får därför
en sökningslänk dit (samma säkra fallback appen redan använder för IMDb när
ID saknas), inte en verifierad direktlänk som de 86 ursprungliga titlarna har.
IMDb-länken blir alltid exakt, eftersom OMDb ger det riktiga IMDb-ID:t.
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta

TMDB_KEY = os.environ.get("TMDB_API_KEY", "")
OMDB_KEY = os.environ.get("OMDB_API_KEY", "")
REGION = "SE"
MIN_IMDB = 7.0
MAX_AGE_YEARS = 10
DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "titles.json")
HISTORY_FILE = os.path.join(os.path.dirname(__file__), "..", "history.json")
HISTORY_MAX_RUNS = 20
SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen_cache.json")

# Urval: titlar upptäcks med flera sökstrategier (se discover_phase) och
# resultatet av varje kollad titel kommer ihåg i seen_cache.json, så samma
# titel inte kostar ett OMDb-anrop varje körning. OMDb:s gratisgräns är
# 1000 anrop/dygn; budgeten nedan lämnar marginal för en extra manuell körning.
MAX_OMDB_PER_RUN = 450
DEEP_PAGES = 25  # 25 sidor x 20 = upp till 500 titlar per tjänst och typ
YEAR_PAGES = 15  # per premiärår: upp till 300 titlar per tjänst och typ
# Hur länge ett kollat resultat gäller innan titeln kollas om (dagar).
SEEN_TTL_DAYS = {"ok": 30, "lowrating": 30, "old": 30, "genre": 90, "nodesc": 14, "nodata": 7}
_OMDB_CALLS = {"n": 0}
_LAST_REASON = {"v": "nodata"}

# Namnen måste matcha hur tjänsterna heter i TMDb:s providerlista.
WANTED_SERVICES = {
    "Netflix": "Netflix",
    "Apple TV+": "Apple TV+",
    "HBO Max": "HBO Max",
    "Prime Video": "Amazon Prime Video",
    "Disney+": "Disney Plus",
}

TMDB_GENRE_BUCKET = {
    # Film
    "Action": "Action & Äventyr", "Adventure": "Action & Äventyr",
    "Animation": "Animerat", "Comedy": "Komedi", "Crime": "Kriminal & Mysterium",
    "Documentary": "Biografi & Sport", "Drama": "Drama", "Family": "Drama",
    "Fantasy": "Sci-fi & Fantasy", "History": "Drama", "Horror": "Skräck",
    "Music": "Musik & Musikal", "Mystery": "Kriminal & Mysterium",
    "Romance": "Komedi", "Science Fiction": "Sci-fi & Fantasy",
    "TV Movie": "Drama", "Thriller": "Thriller", "War": "Drama", "Western": "Action & Äventyr",
    # TV-specifika
    "Action & Adventure": "Action & Äventyr", "Kids": "Drama", "News": "Drama",
    "Reality": "Biografi & Sport", "Sci-Fi & Fantasy": "Sci-fi & Fantasy",
    "Soap": "Drama", "Talk": "Drama", "War & Politics": "Drama",
}


def http_get_json(url, retries=3):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "FILMoSERIER-uppdatering/1.0"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:
                time.sleep(2 * (attempt + 1))
                continue
            print("  HTTP-fel %s för %s" % (e.code, url), file=sys.stderr)
            return None
        except Exception as e:
            print("  Fel vid hämtning: %s" % e, file=sys.stderr)
            if attempt < retries - 1:
                time.sleep(1)
                continue
            return None
    return None


def tmdb_get(path, params):
    params = dict(params)
    params["api_key"] = TMDB_KEY
    url = "https://api.themoviedb.org/3" + path + "?" + urllib.parse.urlencode(params)
    return http_get_json(url)


_GENRE_NAME_CACHE = {}


def get_genre_names(media_type):
    """Hämtar TMDb:s genre-ID->namn en gång per körning (cachat), så vi kan
    slå upp de genre_ids som discover-svaren ger."""
    if media_type in _GENRE_NAME_CACHE:
        return _GENRE_NAME_CACHE[media_type]
    data = tmdb_get("/genre/" + media_type + "/list", {"language": "en-US"})
    names = {g["id"]: g["name"] for g in (data or {}).get("genres", [])}
    _GENRE_NAME_CACHE[media_type] = names
    return names


def get_provider_ids(media_type):
    """Slår upp leverantörs-ID:n dynamiskt via namn, istället för att lita på
    hårdkodade siffror som kan ändras."""
    data = tmdb_get("/watch/providers/" + media_type, {"watch_region": REGION})
    if not data:
        return {}
    by_name = {p["provider_name"]: p["provider_id"] for p in data.get("results", [])}
    ids = {}
    for our_name, tmdb_name in WANTED_SERVICES.items():
        if tmdb_name in by_name:
            ids[our_name] = by_name[tmdb_name]
        else:
            print("  Varning: hittade inte '%s' (%s) i TMDb:s providerlista" % (our_name, tmdb_name), file=sys.stderr)
    return ids


def normalize_length(runtime_min, media_type, seasons=None):
    if media_type == "movie":
        h, m = divmod(runtime_min or 0, 60)
        return ("%d tim %02d min" % (h, m)) if h else ("%d min" % m)
    if seasons and seasons > 1:
        return "Säsong %d" % seasons
    return "1 säsong"


def load_seen():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (IOError, ValueError):
        return {}


def save_seen(seen):
    cutoff = (date.today() - timedelta(days=120)).isoformat()
    keep = {k: v for k, v in seen.items() if isinstance(v, dict) and v.get("d", "") >= cutoff}
    with open(SEEN_FILE, "w", encoding="utf-8") as f:
        json.dump(keep, f, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def seen_fresh(seen, key):
    rec = seen.get(key)
    if not isinstance(rec, dict):
        return False
    ttl = SEEN_TTL_DAYS.get(rec.get("r"), 14)
    try:
        age = (date.today() - date.fromisoformat(rec["d"])).days
    except (KeyError, ValueError):
        return False
    return age < ttl


def _paginate(media_type, params, max_pages):
    results = []
    for page in range(1, max_pages + 1):
        p = dict(params)
        p["page"] = page
        data = tmdb_get("/discover/" + media_type, p)
        if not data:
            break
        results.extend(data.get("results", []))
        if page >= min(data.get("total_pages", 1), 500):
            break
        time.sleep(0.03)
    return results


def discover_phase(media_type, provider_id, phase, year=None):
    """Kompletterande sökstrategier. Enbart "populärast just nu" missar
    välbetygsatta titlar som tonat ut i popularitet (t.ex. en serie som
    avslutades för några månader sedan). TMDb ger dessutom högst 500 träffar
    per sökning, så en enda "bäst röstade"-lista räcker inte heller. Därför:
      0 = populärast just nu (fångar nytt och hett)
      1 = nyast först (fångar färska titlar med få röster)
      2 = flest röster bland välbetygsatta, hela perioden (fångar långkörare
          som premiärade för länge sedan men fortfarande ger nya säsonger)
      3 = flest röster per PREMIÄRÅR (year=...). Varje år får sin egen topp,
          så ingen titel faller bort bara för att perioden som helhet är för
          stor för TMDb:s 500-träffarstak."""
    since = (date.today() - timedelta(days=365 * MAX_AGE_YEARS)).isoformat()
    base = {
        "watch_region": REGION,
        "with_watch_providers": provider_id,
        "with_watch_monetization_types": "flatrate",
        "language": "sv-SE",
    }
    # För TV filtreras INTE på first_air_date i fas 0-2 (bara säsong 1:s
    # premiär - en långkörare som fortfarande ger nya säsonger skulle
    # felaktigt sorteras bort). air_date.gte kollar istället om NÅGOT avsnitt
    # sänts i perioden; exakt recency kollas sedan mot seriens senaste säsong,
    # se get_tv_details().
    if phase == 0:
        params = dict(base, sort_by="popularity.desc")
        if media_type == "movie":
            params["primary_release_date.gte"] = since
        return _paginate(media_type, params, 4)
    params = dict(base)
    date_key = "primary_release_date" if media_type == "movie" else "first_air_date"
    if phase == 3:
        params[date_key + ".gte"] = "%d-01-01" % year
        params[date_key + ".lte"] = "%d-12-31" % year
        params["sort_by"] = "vote_count.desc"
        params["vote_count.gte"] = 10
        params["vote_average.gte"] = 6.5
        return _paginate(media_type, params, YEAR_PAGES)
    if media_type == "movie":
        params["primary_release_date.gte"] = since
    else:
        params["air_date.gte"] = since
    if phase == 1:
        params["sort_by"] = "primary_release_date.desc" if media_type == "movie" else "first_air_date.desc"
        params["vote_count.gte"] = 5
        params["vote_average.gte"] = 6.3
        return _paginate(media_type, params, 10)
    params["sort_by"] = "vote_count.desc"
    params["vote_count.gte"] = 30
    params["vote_average.gte"] = 6.5
    return _paginate(media_type, params, DEEP_PAGES)


def get_imdb_id(media_type, tmdb_id):
    """Exakt IMDb-ID via TMDb. Ger träffsäkrare OMDb-uppslag än titel+år,
    och låter oss känna igen redan sparade titlar utan att spendera OMDb-anrop."""
    if not tmdb_id:
        return ""
    data = tmdb_get("/%s/%s/external_ids" % (media_type, tmdb_id), {}) or {}
    v = data.get("imdb_id") or ""
    return v if re.match(r"^tt\d+$", v) else ""


_TV_LAST_AIR_CACHE = {}


def get_tv_details(tv_id):
    """Hämtar seriens FAKTISKA senaste sändningsdatum (senaste säsongen),
    till skillnad från discover-sökningens first_air_date som bara är
    säsong 1:s premiär, plus en engelsk beskrivning som reserv när TMDb
    saknar svensk text, samt om det finns en kommande/planerad säsong.
    Cachas per körning så samma serie (kan dyka upp via flera tjänster)
    bara slås upp en gång."""
    if tv_id in _TV_LAST_AIR_CACHE:
        return _TV_LAST_AIR_CACHE[tv_id]
    data = tmdb_get("/tv/" + str(tv_id), {}) or {}

    upcoming_season = None  # None = ingen kommande säsong känd
    if data.get("in_production"):
        today = date.today().isoformat()
        for s in data.get("seasons", []):
            if s.get("season_number", 0) == 0:
                continue  # "specials", inte en riktig ny säsong
            air = s.get("air_date")
            if not air or air > today:
                upcoming_season = air or ""  # tom sträng = planerad, okänt datum
                break
        else:
            # in_production är sant men TMDb har inte ens lagt till en post
            # för nästa säsong ännu - vanligt när den bara nyss bekräftats.
            # Vi vet ändå att en till säsong är på gång, bara inte när.
            upcoming_season = ""

    next_ep = data.get("next_episode_to_air") or {}
    next_episode = next_ep.get("air_date")  # None om inget schemalagt känt

    result = {
        "last_air_date": data.get("last_air_date") or data.get("first_air_date"),
        "overview_en": data.get("overview") or "",
        "upcoming_season": upcoming_season,
        "next_episode": next_episode,
    }
    _TV_LAST_AIR_CACHE[tv_id] = result
    return result


_MOVIE_DETAILS_CACHE = {}


def get_movie_overview_en(movie_id):
    """Engelsk beskrivning som reserv för filmer, av samma anledning som
    get_tv_details ovan."""
    if movie_id in _MOVIE_DETAILS_CACHE:
        return _MOVIE_DETAILS_CACHE[movie_id]
    data = tmdb_get("/movie/" + str(movie_id), {}) or {}
    overview = data.get("overview") or ""
    _MOVIE_DETAILS_CACHE[movie_id] = overview
    return overview


def tmdb_find_id(title, year, media_type):
    """Söker upp en titels TMDb-ID via titel + år. Används bara för att
    laga gamla poster i efterhand - vi har bara sparat IMDb-ID, inte
    TMDb-ID, så den vägen måste gås för redan sparade titlar."""
    path = "/search/movie" if media_type == "movie" else "/search/tv"
    data = tmdb_get(path, {"query": title, "language": "sv-SE"})
    if not data:
        return None
    date_field = "release_date" if media_type == "movie" else "first_air_date"
    for r in data.get("results", []):
        if (r.get(date_field) or "")[:4] == str(year):
            return r.get("id")
    results = data.get("results")
    return results[0]["id"] if results else None


def refetch_overview(title, year, kind):
    """Hämtar en hel, korrekt beskrivning på nytt (svenska i första hand,
    engelska som reserv) för en titel som bara finns sparad med IMDb-ID."""
    media_type = "movie" if kind == "film" else "tv"
    tmdb_id = tmdb_find_id(title, year, media_type)
    if not tmdb_id:
        return None
    sv = tmdb_get(("/movie/" if media_type == "movie" else "/tv/") + str(tmdb_id), {"language": "sv-SE"}) or {}
    overview = (sv.get("overview") or "").strip()
    if not overview:
        overview = (get_movie_overview_en(tmdb_id) if media_type == "movie"
                    else get_tv_details(tmdb_id)["overview_en"])
    return overview or None


def truncate_to_sentence(text, max_len=140):
    """Klipper till senaste HELA meningen inom max_len tecken, istället för
    att klippa mitt i en mening. Om inte ens första meningen får plats tas
    hela den ändå med - hellre lite för lång än avklippt mitt i."""
    text = (text or "").strip()
    if not text or len(text) <= max_len:
        return text
    cut = text[:max_len]
    last_end = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
    if last_end != -1:
        return text[:last_end + 1]
    for i, ch in enumerate(text):
        if ch in ".!?":
            return text[:i + 1]
    return text  # ingen punkt alls i hela texten (ovanligt)


def omdb_lookup(title, year, imdb_id=None):
    _OMDB_CALLS["n"] += 1
    if imdb_id:
        url = "https://www.omdbapi.com/?apikey=%s&i=%s" % (OMDB_KEY, imdb_id)
    else:
        url = "https://www.omdbapi.com/?apikey=%s&t=%s&y=%s" % (
            OMDB_KEY, urllib.parse.quote(title), year or "")
    data = http_get_json(url)
    if not data or data.get("Response") == "False":
        return None
    ratings = {r["Source"]: r["Value"] for r in data.get("Ratings", [])}
    try:
        imdb = float(data.get("imdbRating", "N/A"))
    except ValueError:
        return None
    rt = 0
    if "Rotten Tomatoes" in ratings:
        m = re.search(r"(\d+)%", ratings["Rotten Tomatoes"])
        if m:
            rt = int(m.group(1))
    mc = 0
    if "Metacritic" in ratings:
        m = re.search(r"(\d+)", ratings["Metacritic"])
        if m:
            mc = int(m.group(1))
    runtime_min = 0
    m = re.search(r"(\d+)", data.get("Runtime", "") or "")
    if m:
        runtime_min = int(m.group(1))
    seasons = None
    if data.get("totalSeasons") and data["totalSeasons"] not in ("N/A", None):
        try:
            seasons = int(data["totalSeasons"])
        except ValueError:
            pass
    poster = data.get("Poster", "")
    if not poster or poster == "N/A":
        poster = ""
    return {
        "imdb": imdb, "rt": rt, "mc": mc, "imdbID": data.get("imdbID", ""),
        "runtime": runtime_min, "seasons": seasons, "poster": poster,
    }


def build_entry(item, media_type, service_name, known_ids=None, refresh=True):
    """Bygger en post, eller None. Varför en titel avvisades lämnas i
    _LAST_REASON så urvalet kan komma ihåg det (och kolla om senare)."""
    _LAST_REASON["v"] = "nodata"
    title = item.get("title") or item.get("name")
    date_str = item.get("release_date") or item.get("first_air_date")
    if not title or not date_str:
        return None

    # TV-genrer som brukar betyda "alltid färskt" veckoprogram utan en
    # egentlig handling att beskriva (wrestling, pratshower, nyheter) -
    # sorteras bort direkt, innan de dyra uppslagen görs.
    EXCLUDED_TV_GENRES = {10764, 10767, 10763}  # Reality, Talk, News
    if media_type == "tv" and EXCLUDED_TV_GENRES.intersection(item.get("genre_ids", [])):
        _LAST_REASON["v"] = "genre"
        return None

    omdb_year = date_str[:4]  # OMDb indexerar TV-serier på ursprungsåret
    overview_sv = (item.get("overview") or "").strip()
    overview_en = ""
    upcoming_season = None
    next_episode = None

    if media_type == "tv":
        # Kolla mot seriens FAKTISKA senaste sändningsdatum - inte bara
        # säsong 1:s premiär (se kommentar i discover_candidates ovan).
        # Samma anrop ger också en engelsk beskrivning som reserv, samt
        # om det finns en kommande/planerad säsong.
        details = get_tv_details(item.get("id"))
        if not details["last_air_date"]:
            return None
        cutoff = (date.today() - timedelta(days=365 * MAX_AGE_YEARS)).isoformat()
        if details["last_air_date"] < cutoff:
            _LAST_REASON["v"] = "old"
            return None
        date_str = details["last_air_date"]  # visas/sorteras på senaste säsongen, inte premiären
        overview_en = details["overview_en"]
        upcoming_season = details["upcoming_season"]
        next_episode = details["next_episode"]
    elif not overview_sv:
        # Bara hämta den engelska beskrivningen separat om den svenska
        # faktiskt saknas - sparar ett onödigt anrop i normalfallet.
        overview_en = get_movie_overview_en(item.get("id"))

    # TMDb saknar text på BÅDA språken -> troligen inte ett bra "tips" att
    # rekommendera (t.ex. wrestling utan någon egentlig handling), till
    # skillnad från kända serier som bara råkar sakna svensk översättning.
    final_overview = overview_sv or overview_en
    if not final_overview:
        _LAST_REASON["v"] = "nodesc"
        return None

    imdb_id = get_imdb_id(media_type, item.get("id"))
    if known_ids is not None and imdb_id and imdb_id in known_ids and not refresh:
        _LAST_REASON["v"] = "ok"  # finns redan i databasen - inget OMDb-anrop behövs
        return None

    omdb = omdb_lookup(title, omdb_year, imdb_id or None)
    time.sleep(0.15)  # skonsam mot OMDb:s gratisgräns
    if not omdb:
        _LAST_REASON["v"] = "nodata"
        return None
    if omdb["imdb"] < MIN_IMDB:
        _LAST_REASON["v"] = "lowrating"
        return None
    _LAST_REASON["v"] = "ok"

    genre_names = get_genre_names(media_type)
    genre_ids = item.get("genre_ids", [])
    first_genre = genre_names.get(genre_ids[0]) if genre_ids else None
    genre = first_genre if first_genre in TMDB_GENRE_BUCKET or first_genre else "Drama"
    if genre not in TMDB_GENRE_BUCKET:
        genre = "Drama"  # okänd/oöversatt genre -> rimlig standard

    kind = "film" if media_type == "movie" else "serie"
    return {
        "title": title,
        "date": date_str,
        "service": service_name,
        "imdb": omdb["imdb"],
        "rt": omdb["rt"],
        "mc": omdb["mc"],
        "genre": genre,
        "length": normalize_length(omdb["runtime"], media_type, omdb["seasons"]),
        "desc": truncate_to_sentence(final_overview, 140),
        "id": omdb["imdbID"],
        "rtId": "",
        "mcId": "",
        "poster": omdb["poster"],
        "upcomingSeason": upcoming_season,
        "nextEpisode": next_episode,
        "totalSeasons": omdb["seasons"] if media_type == "tv" else None,
        "tmdb": item.get("id"),
        "kind": kind,
    }


def omdb_lookup_by_id(imdb_id):
    """Exakt uppslag via IMDb-ID, utan titel/år-gissning - används för att
    fylla i affischbilder och säsongsantal på titlar som redan finns men
    saknar dem. Returnerar {"poster": str, "seasons": int|None} eller None
    om uppslaget helt misslyckades."""
    _OMDB_CALLS["n"] += 1
    url = "https://www.omdbapi.com/?apikey=%s&i=%s" % (OMDB_KEY, imdb_id)
    data = http_get_json(url)
    if not data or data.get("Response") == "False":
        return None
    poster = data.get("Poster", "")
    if not poster or poster == "N/A":
        poster = ""
    seasons = None
    if data.get("totalSeasons") and data["totalSeasons"] not in ("N/A", None):
        try:
            seasons = int(data["totalSeasons"])
        except ValueError:
            pass
    return {"poster": poster, "seasons": seasons}


def entry_score(x):
    """Grov kvalitetspoäng - används för att avgöra om en nyfunnen post är
    bättre än en redan sparad, så uppenbara luckor kan självläka över tid."""
    s = 0
    if x.get("rtId"): s += 2
    if x.get("mcId"): s += 2
    if x.get("length"): s += 1
    if len(x.get("desc", "")) > 20: s += 1
    return s


def log_history_run(added):
    """Skriver dagens körning överst i historikloggen (senaste först),
    och begränsar loggen till de senaste HISTORY_MAX_RUNS körningarna."""
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
    except (IOError, ValueError):
        history = {"runs": []}

    entry = {
        "date": date.today().isoformat(),
        "added": [
            {"title": it["title"], "kind": it["kind"], "date": it["date"]}
            for it in added["film"] + added["serie"]
        ],
    }
    history["runs"] = [entry] + history.get("runs", [])
    history["runs"] = history["runs"][:HISTORY_MAX_RUNS]

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, separators=(",", ":"))


def main():
    if not TMDB_KEY or not OMDB_KEY:
        print("TMDB_API_KEY eller OMDB_API_KEY saknas som miljövariabel/secret. Avbryter.", file=sys.stderr)
        sys.exit(1)

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        current = json.load(f)

    by_id = {}
    existing_keys = set()
    for kind in ("film", "serie"):
        for it in current.get(kind, []):
            if it.get("id"):
                by_id[it["id"]] = it
            existing_keys.add(kind + ":" + it["title"] + ":" + it["date"])

    added = {"film": [], "serie": []}
    upgraded = 0

    # Backfill: fyll i affischbilder och/eller totalt säsongsantal på
    # titlar som redan finns men saknar dem (poster: lades in innan det
    # fältet fanns; totalSeasons: nytt fält för säsongsframsteg). Samma
    # IMDb-ID-uppslag ger båda, så de körs i ett gemensamt pass - skonsamt
    # mot OMDb:s gratisgräns (163 titlar ryms gott och väl inom 1000/dag).
    needs_omdb_backfill = [
        it for it in by_id.values()
        if not it.get("poster") or (it.get("kind") == "serie" and "totalSeasons" not in it)
    ]
    if needs_omdb_backfill:
        print("Fyller i affisch/säsongsantal för %d titlar som saknar dem..." % len(needs_omdb_backfill))
        for it in needs_omdb_backfill:
            result = omdb_lookup_by_id(it["id"])
            time.sleep(0.15)
            if not result:
                continue
            changed = False
            if result["poster"] and not it.get("poster"):
                it["poster"] = result["poster"]
                changed = True
            if it.get("kind") == "serie" and "totalSeasons" not in it:
                it["totalSeasons"] = result["seasons"]  # kan vara None, det är okej
                changed = True
            if changed:
                upgraded += 1
                print("  ~ affisch/säsonger: %s" % it["title"])

    # Backfill: laga beskrivningar som klipptes av mitt i en mening av den
    # gamla koden, innan truncate_to_sentence fanns. Går via en titel/år-
    # sökning på TMDb eftersom bara IMDb-ID sparades från början.
    broken_desc = [it for it in by_id.values()
                   if it.get("desc") and not it["desc"].rstrip().endswith((".", "!", "?"))]
    if broken_desc:
        print("Lagar %d beskrivningar avklippta mitt i en mening..." % len(broken_desc))
        for it in broken_desc:
            fresh = refetch_overview(it["title"], it["date"][:4], it["kind"])
            time.sleep(0.1)
            if fresh:
                new_desc = truncate_to_sentence(fresh, 140)
                if new_desc.rstrip().endswith((".", "!", "?")):
                    it["desc"] = new_desc
                    upgraded += 1
                    print("  ~ beskrivning: %s" % it["title"])

    # Kollar kommande/planerad säsong OCH nästa avsnitts sändningsdatum i
    # samma pass (ett TMDb-uppslag per serie istället för två separata).
    # upcomingSeason kollas bara en gång (fältet saknas = aldrig kollad).
    # nextEpisode uppdateras varje körning för alla pågående serier,
    # eftersom den datan blir inaktuell så fort avsnittet sänts.
    today_str = date.today().isoformat()
    needs_check = [
        it for it in by_id.values()
        if it.get("kind") == "serie" and (
            "upcomingSeason" not in it
            or (it.get("upcomingSeason") is not None and (
                "nextEpisode" not in it
                or not it.get("nextEpisode")
                or it["nextEpisode"] < today_str
            ))
        )
    ]
    if needs_check:
        print("Kollar kommande säsong/nästa avsnitt för %d serier..." % len(needs_check))
        for it in needs_check:
            tmdb_id = tmdb_find_id(it["title"], it["date"][:4], "tv")
            time.sleep(0.1)
            if tmdb_id:
                details = get_tv_details(tmdb_id)
                if "upcomingSeason" not in it:
                    it["upcomingSeason"] = details["upcoming_season"]
                    if details["upcoming_season"] is not None:
                        print("  ~ kommande säsong: %s" % it["title"])
                if it.get("nextEpisode") != details["next_episode"]:
                    it["nextEpisode"] = details["next_episode"]
                    if details["next_episode"]:
                        print("  ~ nästa avsnitt: %s (%s)" % (it["title"], details["next_episode"]))
                upgraded += 1
            # Annars: lämna fälten osatta/oförändrade - en TILLFÄLLIGT
            # misslyckad sökning ska inte permanent stämpla något, och
            # nästa körning försöker på nytt istället.

    # --- Urval ---
    today_s = date.today().isoformat()
    seen = load_seen()
    providers = {mt: get_provider_ids(mt) for mt in ("movie", "tv")}
    known_ids = set(by_id.keys())

    # Planen byggs fas för fas (populärast, nyast, djupsökning) över alla
    # tjänster, så det viktigaste kollas först om OMDb-budgeten tar slut.
    this_year = date.today().year
    phases = [(0, None, "populärast just nu"), (1, None, "nyast först")]
    for y in range(this_year, this_year - MAX_AGE_YEARS - 1, -1):
        phases.append((3, y, "premiärår %d" % y))
    phases.append((2, None, "djupsökning, hela perioden"))
    plan = []
    queued = set()
    for phase, year, phase_name in phases:
        for media_type in ("movie", "tv"):
            for service_name, provider_id in providers[media_type].items():
                found = discover_phase(media_type, provider_id, phase, year)
                new_in_plan = 0
                for item in found:
                    k = "%s:%s" % (media_type, item.get("id"))
                    if k in queued:
                        continue
                    queued.add(k)
                    plan.append((media_type, service_name, item, k))
                    new_in_plan += 1
                print(" [%s] %s / %s: %d kandidater, %d nya i planen" % (
                    phase_name, media_type, service_name, len(found), new_in_plan))

    print("Plan: %d unika kandidater." % len(plan))
    skipped_fresh = 0
    checked = 0
    deferred = 0
    for idx, (media_type, service_name, item, k) in enumerate(plan):
        if seen_fresh(seen, k):
            skipped_fresh += 1
            continue
        if _OMDB_CALLS["n"] >= MAX_OMDB_PER_RUN:
            deferred = sum(1 for p in plan[idx:] if not seen_fresh(seen, p[3]))
            print("OMDb-budgeten för körningen (%d anrop) är slut. %d kandidater väntar till nästa körning." % (
                MAX_OMDB_PER_RUN, deferred))
            break
        entry = build_entry(item, media_type, service_name, known_ids=known_ids, refresh=(k in seen))
        checked += 1
        seen[k] = {"d": today_s, "r": "ok" if entry else _LAST_REASON["v"]}
        if not entry:
            continue

        # IMDb-ID är den pålitliga nyckeln - releasedatum kan skilja
        # sig med några dagar mellan TMDb och det datum en titel
        # faktiskt dök upp på tjänsten, vilket annars gett dubbletter.
        if entry["id"] and entry["id"] in by_id:
            old = by_id[entry["id"]]
            # Poster och kommande säsong uppdateras oberoende av
            # betygsjämförelsen nedan - annars kunde en färskare
            # affisch eller nytt säsongsdatum tystas ner bara för
            # att RT/MC/beskrivning råkade vara oförändrade.
            refreshed = False
            if entry.get("poster") and entry["poster"] != old.get("poster"):
                old["poster"] = entry["poster"]
                refreshed = True
            if "upcomingSeason" in entry and entry["upcomingSeason"] != old.get("upcomingSeason"):
                old["upcomingSeason"] = entry["upcomingSeason"]
                refreshed = True
            if entry.get("totalSeasons") and entry["totalSeasons"] != old.get("totalSeasons"):
                old["totalSeasons"] = entry["totalSeasons"]
                refreshed = True
            if entry.get("tmdb") and not old.get("tmdb"):
                old["tmdb"] = entry["tmdb"]
                refreshed = True
            if entry_score(entry) > entry_score(old):
                old["imdb"], old["rt"], old["mc"] = entry["imdb"], entry["rt"], entry["mc"]
                if entry["length"]:
                    old["length"] = entry["length"]
                if len(entry["desc"]) > len(old.get("desc", "")):
                    old["desc"] = entry["desc"]
                refreshed = True
                print("  ~ uppdaterade %s med bättre data" % entry["title"])
            if refreshed:
                upgraded += 1
            continue

        key = entry["kind"] + ":" + entry["title"] + ":" + entry["date"]
        if key in existing_keys:
            continue
        if entry["id"]:
            by_id[entry["id"]] = entry
        existing_keys.add(key)
        added[entry["kind"]].append(entry)
        print("  + %s (%s) IMDb %.1f" % (entry["title"], entry["date"][:4], entry["imdb"]))

    save_seen(seen)
    print("Urval klart: %d kollade, %d hoppades över (nyligen kollade), %d väntar. OMDb-anrop: %d." % (
        checked, skipped_fresh, deferred, _OMDB_CALLS["n"]))

    if added["film"] or added["serie"]:
        log_history_run(added)

    if not added["film"] and not added["serie"] and not upgraded:
        print("Inga nya titlar eller uppdateringar den här veckan.")
        return

    current["film"] = current.get("film", []) + added["film"]
    current["serie"] = current.get("serie", []) + added["serie"]
    current["updated"] = date.today().isoformat()

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(current, f, ensure_ascii=False, separators=(",", ":"))

    print("Klart: +%d filmer, +%d serier, %d poster uppgraderade." % (
        len(added["film"]), len(added["serie"]), upgraded))


if __name__ == "__main__":
    main()
