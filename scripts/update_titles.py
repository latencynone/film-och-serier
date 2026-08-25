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


def discover_candidates(media_type, provider_id):
    since = (date.today() - timedelta(days=365 * MAX_AGE_YEARS)).isoformat()
    params_base = {
        "watch_region": REGION,
        "with_watch_providers": provider_id,
        "with_watch_monetization_types": "flatrate",
        "sort_by": "popularity.desc",
        "language": "sv-SE",
    }
    if media_type == "movie":
        # En films releasedatum är entydigt - filtrera direkt i sökningen.
        params_base["primary_release_date.gte"] = since
    # För TV filtreras INTE på discover-sökningens first_air_date, eftersom
    # det bara är säsong 1:s premiärdatum. En långkörare som fortfarande
    # sänder nya säsonger (t.ex. The Boys, premiär 2019, sista säsongen 2026)
    # skulle annars felaktigt sorteras bort som "för gammal". Recency
    # kollas istället per kandidat mot seriens FAKTISKA senaste säsong,
    # se get_tv_details().
    results = []
    for page in (1, 2):
        params = dict(params_base)
        params["page"] = page
        data = tmdb_get("/discover/" + media_type, params)
        if not data:
            break
        results.extend(data.get("results", []))
        if page >= data.get("total_pages", 1):
            break
    return results


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

    result = {
        "last_air_date": data.get("last_air_date") or data.get("first_air_date"),
        "overview_en": data.get("overview") or "",
        "upcoming_season": upcoming_season,
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


def omdb_lookup(title, year):
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


def build_entry(item, media_type, service_name):
    title = item.get("title") or item.get("name")
    date_str = item.get("release_date") or item.get("first_air_date")
    if not title or not date_str:
        return None

    # TV-genrer som brukar betyda "alltid färskt" veckoprogram utan en
    # egentlig handling att beskriva (wrestling, pratshower, nyheter) -
    # sorteras bort direkt, innan de dyra uppslagen görs.
    EXCLUDED_TV_GENRES = {10764, 10767, 10763}  # Reality, Talk, News
    if media_type == "tv" and EXCLUDED_TV_GENRES.intersection(item.get("genre_ids", [])):
        return None

    omdb_year = date_str[:4]  # OMDb indexerar TV-serier på ursprungsåret
    overview_sv = (item.get("overview") or "").strip()
    overview_en = ""
    upcoming_season = None

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
            return None
        date_str = details["last_air_date"]  # visas/sorteras på senaste säsongen, inte premiären
        overview_en = details["overview_en"]
        upcoming_season = details["upcoming_season"]
    elif not overview_sv:
        # Bara hämta den engelska beskrivningen separat om den svenska
        # faktiskt saknas - sparar ett onödigt anrop i normalfallet.
        overview_en = get_movie_overview_en(item.get("id"))

    # TMDb saknar text på BÅDA språken -> troligen inte ett bra "tips" att
    # rekommendera (t.ex. wrestling utan någon egentlig handling), till
    # skillnad från kända serier som bara råkar sakna svensk översättning.
    final_overview = overview_sv or overview_en
    if not final_overview:
        return None

    omdb = omdb_lookup(title, omdb_year)
    time.sleep(0.15)  # skonsam mot OMDb:s gratisgräns
    if not omdb or omdb["imdb"] < MIN_IMDB:
        return None

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
        "kind": kind,
    }


def omdb_lookup_by_id(imdb_id):
    """Exakt uppslag via IMDb-ID, utan titel/år-gissning - används för att
    fylla i affischbilder på titlar som redan finns men saknar en."""
    url = "https://www.omdbapi.com/?apikey=%s&i=%s" % (OMDB_KEY, imdb_id)
    data = http_get_json(url)
    if not data or data.get("Response") == "False":
        return None
    poster = data.get("Poster", "")
    if not poster or poster == "N/A":
        return None
    return poster


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

    # Backfill: fyll i affischbilder på titlar som redan finns men lades in
    # innan poster-fältet fanns. Exakt uppslag per IMDb-ID, skonsamt mot
    # OMDb:s gratisgräns (163 titlar ryms gott och väl inom 1000/dag).
    missing_poster = [it for it in by_id.values() if not it.get("poster")]
    if missing_poster:
        print("Fyller i affischbilder för %d titlar utan en sedan tidigare..." % len(missing_poster))
        for it in missing_poster:
            poster = omdb_lookup_by_id(it["id"])
            time.sleep(0.15)
            if poster:
                it["poster"] = poster
                upgraded += 1
                print("  ~ affisch: %s" % it["title"])

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

    # Backfill: kolla om det finns en kommande/planerad säsong för serier
    # som lades till innan upcomingSeason-fältet fanns.
    missing_upcoming = [it for it in by_id.values()
                         if it.get("kind") == "serie" and "upcomingSeason" not in it]
    if missing_upcoming:
        print("Kollar kommande säsonger för %d serier..." % len(missing_upcoming))
        for it in missing_upcoming:
            tmdb_id = tmdb_find_id(it["title"], it["date"][:4], "tv")
            time.sleep(0.1)
            if tmdb_id:
                details = get_tv_details(tmdb_id)
                it["upcomingSeason"] = details["upcoming_season"]
                upgraded += 1
                if details["upcoming_season"] is not None:
                    print("  ~ kommande säsong: %s" % it["title"])
            # Annars: lämna fältet osatt - annars skulle en TILLFÄLLIGT
            # misslyckad sökning permanent stämplas som "ingen kommande
            # säsong", och titeln skulle aldrig kollas igen. Nu försöker
            # nästa körning på nytt istället.

    for media_type in ("movie", "tv"):
        print("== %s ==" % media_type)
        provider_ids = get_provider_ids(media_type)
        for service_name, provider_id in provider_ids.items():
            print(" Söker på %s..." % service_name)
            candidates = discover_candidates(media_type, provider_id)
            for item in candidates:
                entry = build_entry(item, media_type, service_name)
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
