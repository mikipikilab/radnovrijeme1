from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, abort
from datetime import datetime
from zoneinfo import ZoneInfo
from email.utils import formataddr

import json, os
# NOVO:
import smtplib, ssl, csv
from email.message import EmailMessage

app = Flask(__name__, template_folder="templates")

# --- podesiva putanja za CSV (na Renderu koristi /data/poruke.csv) ---
CSV_PATH = os.environ.get("CSV_PATH") or os.path.join(app.instance_path, "poruke.csv")

# --- CSV init: napravi fajl sa headerom ako ne postoji ---
def ensure_csv():
    try:
        dirpath = os.path.dirname(CSV_PATH)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        if not os.path.exists(CSV_PATH):
            with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
                csv.writer(f).writerow(["datetime", "ime", "kontakt", "ip", "poruka"])
    except Exception as e:
        print(f"CSV init error: {e}", flush=True)

ensure_csv()

# Dodaj posebna pravila za utorak i subotu
RADNO_VRIJEME = {
    "ponedjeljak": {"start": 10, "end": 20},
    "utorak": {"start": 10, "end": 20},
    "srijeda": {"start": 10, "end": 20},
    "četvrtak": {"start": 10, "end": 20},
    "petak": {"start": 10, "end": 20},
    "subota": {"start": 10, "end": 14},
    "nedjelja": None
}

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_FILE = os.path.join(BASE_DIR, "data.json")

DANI_PUNIM = ["Ponedjeljak", "Utorak", "Srijeda", "Četvrtak", "Petak", "Subota", "Nedjelja"]

def now_podgorica():
    try:
        return datetime.now(ZoneInfo("Europe/Podgorica"))
    except Exception:
        return datetime.now()  # fallback ako nema tzdata

def ucitaj_posebne_datume():
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def sacuvaj_posebne_datume(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def to_int_or_none(x):
    try:
        return int(x)
    except (ValueError, TypeError):
        return None

def sat_label(h):
    try:
         isinstance(h, float) and not h.is_integer():
            sati = int(h)
            minuti = int((h - sati) * 60)
            return f"{sati}:{minuti:02d}"
        return str(int(h))
    except Exception:
        return str(h)

@app.route("/")
def index():
    sada = now_podgorica()
    dan = sada.weekday()
    ime_dana = DANI_PUNIM[dan].lower()

    sv = RADNO_VRIJEME.get(ime_dana)
     sv is None:
        start, end = None, None
    else:
        start, end = sv["start"], sv["end"]

    posebni = ucitaj_posebne_datume()
    datum_str = sada.strftime("%Y-%m-%d")
    ps = posebni.get(datum_str)
     isinstance(ps, (list, tuple)) and len(ps) == 2:
        start = ps[0]  ps[0] is not None else None
        end   = ps[1]  ps[1] is not None else None

    # odredi status i poruku
     start is None or end is None:
        poruka_html = "Danas je neradni dan."
        poruka_tts  = "Danas je neradni dan."
        status_slika = "close1.png"
    else:
        sat = sada.hour + sada.minute / 60
        otvoreno_sad = (start <= sat < end)
         otvoreno_sad:
            linije = [
                "Ordinacija je trenutno otvorena.",
                f"Danas je radno vrijeme od {sat_label(start)} do {sat_label(end)} časova."
            ]
            status_slika = "open.png"
        else:
            linije = [
                "Ordinacija je trenutno zatvorena.",
                f"Danas je radno vrijeme od {sat_label(start)} do {sat_label(end)} časova."
            ]
            status_slika = "close1.png"

        poruka_html = "<br>".join(linije)
        poruka_tts  = " ".join(linije)

    poruka_upper = poruka_html.upper()

    return render_template(
        "index.html",
        poruka_upper=poruka_upper,
        poruka=poruka_html,
        poruka_tts=poruka_tts,
        status_slika=status_slika
    )

@app.route("/admin", methods=["GET", "POST"])
def admin():
    posebni = ucitaj_posebne_datume()
     request.method == "POST":
        datum = request.form["datum"].strip()
         "neradni" in request.form:
            start = end = None
        else:
            start = to_int_or_none(request.form.get("start"))
            end   = to_int_or_none(request.form.get("end"))
             start is None or end is None:
                start = end = None
        posebni[datum] = [start, end]
        sacuvaj_posebne_datume(posebni)
        return redirect(url_for("admin"))

    sortirano = dict(sorted(posebni.items()))
    return render_template("admin.html", posebni=sortirano)

@app.route("/obrisi/<datum>")
def obrisi(datum):
    posebni = ucitaj_posebne_datume()
     datum in posebni:
        del posebni[datum]
        sacuvaj_posebne_datume(posebni)
    return redirect(url_for("admin"))

@app.route("/posalji_poruku", methods=["POST"])
def posalji_poruku():
    data = request.get_json(force=True, silent=True) or {}
    ime     = (data.get("ime") or "").strip()
    kontakt = (data.get("kontakt") or "").strip()
    poruka  = (data.get("poruka") or "").strip()

     not poruka:
        return jsony(ok=False, error="Poruka je obavezna."), 400

    now = now_podgorica()

    body = (
        f"Ime i prezime: {ime or '—'}\n"
        f"Kontakt: {kontakt or '—'}\n\n"
        f"Poruka:\n{poruka}\n\n"
        f"Vrijeme: {now.isoformat()}\n"
        f"IP: {request.remote_addr}\n"
    )

    # 1) Arhiva u CSV
    try:
        newfile = not os.path.exists(CSV_PATH)
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
             newfile:
                w.writerow(["datetime", "ime", "kontakt", "ip", "poruka"])
            w.writerow([now.isoformat(), ime, kontakt, request.remote_addr, poruka])
    except Exception as e:
        print(f"CSV write error: {e}", flush=True)
        # nastavljamo na slanje maila

    # 2) Slanje email-a (env varijable)
    user   = os.environ.get("GMAIL_USER")
    app_pw = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")

     not user or not app_pw:
        return jsony(ok=True, warning="Mail nije poslat (GMAIL_USER/GMAIL_APP_PASSWORD nisu postavljeni)."), 200

    try:
        msg = EmailMessage()
        msg["From"] = formataddr(("PORUKA SA VRATA", user))  # npr. "PORUKA SA VRATA <dentalabplaner@gmail.com>"
        msg["To"] = "dentalabplaner@gmail.com"
        msg["Subject"] = f"[Kontakt sa sajta] {ime or 'Anonimno'} — {now.strftime('%d.%m.%Y %H:%M')}"
        msg.set_content(body)

        # Ako je korisnik upisao e-mail u "kontakt", Reply-To na njega:
        if kontakt and "@" in kontakt:
            msg["Reply-To"] = kontakt

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as smtp:
            smtp.login(user, app_pw)
            smtp.send_message(msg)

    except Exception as e:
        print(f"Mail error: {e}", flush=True)
        return jsonify(ok=True, warning=f"CSV sačuvan, ali slanje maila nije uspjelo: {type(e).__name__}"), 200

    return jsonify(ok=True), 200

# --- Pomoćne rute za CSV (opciono) ---
@app.get("/csv_debug")
def csv_debug():
    exists = os.path.exists(CSV_PATH)
    size = os.path.getsize(CSV_PATH) if exists else 0
    return jsonify(path=CSV_PATH, exists=exists, size=size)

@app.get("/poruke.csv")
def download_csv():
    if not os.path.exists(CSV_PATH):
        abort(404)
    return send_file(CSV_PATH, as_attachment=True, download_name="poruke.csv")
@app.get("/ping")
def ping():
    return "ok", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5098))
    app.run(host="0.0.0.0", port=port, debug=True)
