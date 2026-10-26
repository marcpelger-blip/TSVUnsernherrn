#!/usr/bin/env python3
"""
Spieltagsvorschau für Instagram, läuft kostenlos auf GitHub Actions.

  python main.py render   nächstes Spiel aus dem Google Sheet holen, Bilder und Text erstellen
  python main.py post     die zuletzt erstellten Bilder als Karussell auf Instagram veröffentlichen
"""
import base64
import csv
import io
import json
import os
import random
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

# ---------- Hier einmal anpassen ----------
VEREIN_NAME = "SV Beispiel"
VEREIN_KUERZEL = "SB"
VEREIN_LOGO = "logo_verein.png"
HASHTAGS = "#SVBeispiel #Fussball #Spieltag"
VORLAUF_TAGE = 7                    # gepostet wird, wenn das Spiel in den nächsten 7 Tagen liegt
GRAPH_VERSION = "v25.0"             # bei Fehlern die aktuelle Version aus den Meta-Docs eintragen
GEMINI_MODEL = "gemini-2.5-flash"   # bei "model not found" hier ein aktuelles Modell eintragen
# ------------------------------------------

ROOT = Path(__file__).resolve().parent
POSTS = ROOT / "posts"
MERKLISTE = ROOT / "gepostet.json"
GRAPH = f"https://graph.facebook.com/{GRAPH_VERSION}"
WOCHENTAGE = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONATE = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember"]
MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}
VORLAGEN = [
    "Spieltag! 🔴⚫ {art} gegen {gegner}, {datum} um {zeit} Uhr. Wir freuen uns auf euch, kommt vorbei und macht Stimmung!\n\n{tags}",
    "Es ist so weit: {gegner}, {datum}, {zeit} Uhr. Wir geben alles, ihr bringt die Stimmung mit? 💪\n\n{tags}",
    "{datum}, {zeit} Uhr: {art} gegen {gegner}. Zeit zusammenzustehen, wir sehen uns am Platz! ⚽\n\n{tags}",
]


# ---------- Spielplan ----------

def parse_datum(text):
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Datum nicht lesbar: {text!r} (Beispiel: 14.11.2026)")


def parse_zeit(text):
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M:%S %p"):
        try:
            return datetime.strptime(text.upper(), fmt).strftime("%H:%M")
        except ValueError:
            pass
    raise ValueError(f"Uhrzeit nicht lesbar: {text!r} (Beispiel: 15:30)")


def spiele_laden():
    text = requests.get(os.environ["SPIELPLAN_CSV_URL"], timeout=30).content.decode("utf-8-sig")
    kopf = text.split("\n", 1)[0]
    trenner = ";" if kopf.count(";") > kopf.count(",") else ","
    spiele = []
    for zeile in csv.DictReader(io.StringIO(text), delimiter=trenner):
        z = {(k or "").strip().lower(): (v or "").strip() for k, v in zeile.items()}
        if not z.get("datum") or not z.get("gegner"):
            continue
        spiele.append({
            "datum": parse_datum(z["datum"]),
            "uhrzeit": parse_zeit(z["uhrzeit"]) if z.get("uhrzeit") else "",
            "gegner": z["gegner"],
            "heim": z.get("heimspiel", "").lower() in ("ja", "j", "x", "true", "1"),
            "logo": z.get("logo", ""),
            "liga": z.get("liga", ""),
            "ort": z.get("ort", ""),
            "adresse": z.get("adresse", ""),
        })
    return spiele


def schluessel(spiel):
    return f"{spiel['datum'].isoformat()}|{spiel['gegner']}"


def naechstes_spiel(spiele, gepostet):
    heute = date.today()
    passend = [
        s for s in spiele
        if heute <= s["datum"] <= heute + timedelta(days=VORLAUF_TAGE)
        and schluessel(s) not in gepostet
    ]
    return min(passend, key=lambda s: s["datum"], default=None)


def datum_text(d):
    return f"{WOCHENTAGE[d.weekday()]}, {d.day}. {MONATE[d.month - 1]}"


# ---------- Text ----------

def schreibe_text(spiel):
    art = "Heimspiel" if spiel["heim"] else "Auswärtsspiel"
    datum = datum_text(spiel["datum"])
    zeit = spiel["uhrzeit"] or "–"
    api_key = os.environ.get("GEMINI_API_KEY", "")
    if api_key:
        ort = f" Spielort: {spiel['ort']}." if spiel["ort"] else ""
        prompt = (
            f"Schreibe die Instagram-Beschreibung für die Spieltagsvorschau von {VEREIN_NAME}. "
            f"Spiel: {art} gegen {spiel['gegner']}, {datum}, Anstoß {zeit} Uhr.{ort} "
            f"Liga: {spiel['liga'] or 'unbekannt'}. "
            "Vorgaben: Deutsch, locker und motivierend, höchstens 300 Zeichen Fließtext, "
            "ein bis zwei passende Emojis, am Ende ein kurzer Aufruf zum Vorbeikommen. "
            f"Hänge diese Hashtags an: {HASHTAGS}. "
            "Gib ausschließlich den fertigen Text aus, ohne Einleitung und ohne Anführungszeichen."
        )
        try:
            antwort = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                params={"key": api_key},
                json={"contents": [{"parts": [{"text": prompt}]}]},
                timeout=60,
            )
            antwort.raise_for_status()
            teile = antwort.json()["candidates"][0]["content"]["parts"]
            text = "".join(t.get("text", "") for t in teile).replace("**", "").strip()
            if text:
                return text[:2200]
        except Exception as fehler:
            print(f"KI-Text nicht verfügbar, nehme Vorlage: {fehler}")
    vorlage = random.choice(VORLAGEN)
    return vorlage.format(art=art, gegner=spiel["gegner"], datum=datum, zeit=zeit, tags=HASHTAGS)


# ---------- Bilder ----------

def logo_uri(dateiname):
    if not dateiname:
        return ""
    pfad = ROOT / dateiname
    if not pfad.is_file():
        print(f"Warnung: Logo '{dateiname}' fehlt, es wird das Kürzel gezeigt.")
        return ""
    mime = MIME.get(pfad.suffix.lower(), "image/png")
    return f"data:{mime};base64,{base64.b64encode(pfad.read_bytes()).decode()}"


def kuerzel(name):
    return "".join(wort[0] for wort in name.split()[:3]).upper()


def render_bilder(spiel):
    d = spiel["datum"]
    heim = spiel["heim"]
    heim_name = VEREIN_NAME if heim else spiel["gegner"]
    gast_name = spiel["gegner"] if heim else VEREIN_NAME
    kontext = {
        "liga": spiel["liga"],
        "spielart": "Heimspiel" if heim else "Auswärtsspiel",
        "heim": heim,
        "wochentag": WOCHENTAGE[d.weekday()],
        "datum_kurz": f"{d.day:02d}.{d.month:02d}.",
        "datum_lang": f"{d.day}. {MONATE[d.month - 1]}",
        "uhrzeit": spiel["uhrzeit"] or "–",
        "ort": spiel["ort"],
        "adresse": spiel["adresse"],
        "heim_name": heim_name,
        "gast_name": gast_name,
        "heim_logo": logo_uri(VEREIN_LOGO if heim else spiel["logo"]),
        "gast_logo": logo_uri(spiel["logo"] if heim else VEREIN_LOGO),
        "heim_kuerzel": kuerzel(heim_name),
        "gast_kuerzel": kuerzel(gast_name),
        "verein_logo": logo_uri(VEREIN_LOGO),
        "verein_kuerzel": VEREIN_KUERZEL,
    }
    POSTS.mkdir(exist_ok=True)
    env = Environment(loader=FileSystemLoader(ROOT), autoescape=True)
    dateien = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        seite = browser.new_page(viewport={"width": 1080, "height": 1350}, device_scale_factor=1)
        for nr, vorlage in enumerate(["bild1.html", "bild2.html"], start=1):
            seite.set_content(env.get_template(vorlage).render(**kontext), wait_until="networkidle")
            seite.evaluate("document.fonts.ready.then(() => true)")
            name = f"{d.isoformat()}_{nr}.jpg"
            seite.screenshot(path=str(POSTS / name), type="jpeg", quality=92)
            dateien.append(name)
        browser.close()
    return dateien


# ---------- Ablauf ----------

def modus_render():
    spiele = spiele_laden()
    gepostet = set(json.loads(MERKLISTE.read_text(encoding="utf-8"))) if MERKLISTE.exists() else set()
    spiel = naechstes_spiel(spiele, gepostet)
    if spiel is None:
        print("Kein Spiel in den nächsten Tagen, heute gibt es nichts zu posten.")
        return
    dateien = render_bilder(spiel)
    text = schreibe_text(spiel)
    (POSTS / "aktuell.json").write_text(
        json.dumps({"schluessel": schluessel(spiel), "bilder": dateien, "text": text},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"Vorbereitet: {spiel['gegner']} am {spiel['datum']}\n\n{text}")
    if "GITHUB_OUTPUT" in os.environ:
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            f.write("neu=true\n")


def graph(methode, pfad, **felder):
    felder["access_token"] = os.environ["IG_TOKEN"]
    antwort = requests.request(methode, f"{GRAPH}/{pfad}", data=felder, timeout=60)
    if not antwort.ok:
        raise SystemExit(f"Graph-API-Fehler bei {pfad}: {antwort.text}")
    return antwort.json()


def warte_bis_fertig(container_id):
    for _ in range(30):
        status = requests.get(
            f"{GRAPH}/{container_id}",
            params={"fields": "status_code", "access_token": os.environ["IG_TOKEN"]},
            timeout=30,
        ).json()
        if "error" in status:
            raise SystemExit(f"Instagram meldet einen Fehler: {status['error']}")
        code = status.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise SystemExit(f"Instagram konnte den Container nicht verarbeiten: {status}")
        time.sleep(10)
    raise SystemExit("Instagram hat zu lange gebraucht.")


def modus_post():
    daten = json.loads((POSTS / "aktuell.json").read_text(encoding="utf-8"))
    basis = (f"https://raw.githubusercontent.com/{os.environ['GITHUB_REPOSITORY']}"
             f"/{os.environ['COMMIT_SHA']}/posts")
    ig = os.environ["IG_USER_ID"]

    kinder = []
    for datei in daten["bilder"]:
        kind = graph("POST", f"{ig}/media", image_url=f"{basis}/{datei}", is_carousel_item="true")
        kinder.append(kind["id"])
    for kind_id in kinder:
        warte_bis_fertig(kind_id)

    karussell = graph("POST", f"{ig}/media", media_type="CAROUSEL",
                      children=",".join(kinder), caption=daten["text"])
    warte_bis_fertig(karussell["id"])
    graph("POST", f"{ig}/media_publish", creation_id=karussell["id"])

    merkliste = json.loads(MERKLISTE.read_text(encoding="utf-8")) if MERKLISTE.exists() else []
    merkliste.append(daten["schluessel"])
    MERKLISTE.write_text(json.dumps(merkliste, indent=2), encoding="utf-8")
    print("Auf Instagram veröffentlicht.")


if __name__ == "__main__":
    befehl = sys.argv[1] if len(sys.argv) > 1 else ""
    if befehl == "render":
        modus_render()
    elif befehl == "post":
        modus_post()
    else:
        raise SystemExit("Aufruf: python main.py render  oder  python main.py post")
