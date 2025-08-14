from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, abort
from datetime import datetime
from zoneinfo import ZoneInfo
from email.utils import formataddr

import json, os, re
import smtplib, ssl, csv
from email.message import EmailMessage

app = Flask(__name__, template_folder="templates")

# --- podesiva putanja za CSV (na Renderu koristi /data/poruke.csv) ---
CSV_PATH = os.environ.get("CSV_PATH") or os.path.join(app.instance_path, "poruke.csv")
DEFAULT_COUNTRY_CODE = os.environ.get("DEFAULT_COUNTRY_CODE", "+382")

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

# Default radno vrijeme
RADNO_VRIJEME = {
    "ponedjeljak": {"start": 10, "end": 20},
    "utorak":      {"start": 10, "end": 20},
    "srijeda":     {"start": 10, "end": 20},
    "četvrtak":    {"start": 10, "end": 20},
    "petak":       {"start": 10, "end": 20},
    "subota":      {"start": 10, "end": 14},
    "nedjelja":    None
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
            data = json.load(f)
            if isinstance(data, dict):
                return data
            return {}
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

def to_minutes(h):
    """Za provjeru otvoreno/zatvoreno koristimo minute, zaokružene na najbliži minut."""
    if h is None:
        return None
    try:
        return int(round(float(h) * 60))
    except Exception:
        return None

def sat_label(h):
    """
    Format:
    - pun sat -> 'HH'
    - ima minuta -> 'HH:MM'
    """
    try:
        if h is None:
            return ""
        val = float(h)
        sati = int(val)
        minuti = int(round((val - sati) * 60))
        if minuti == 0:
            return str(sati)
        return f"{sati}:{minuti:02d}"
    except Exception:
        return str(h)

# --- kontakt helperi ---
def is_email(s):
    if not s:
        return False
    return "@" in s and "." in s.split("@")[-1]

def normalize_phone(raw):
    """
    Normalizuje telefon:
    - dozvoljava cifre i '+'
    - 00xx pretvara u +xx
    - ako počinje nulom bez '+', a DEFAULT_COUNTRY_CODE je postavljen -> doda se pozivni
    - vraća None ako nema najmanje 7 cifara
    """
    if not raw:
        return None

    # zadrži samo + i cifre
    s = re.sub(r"[^\d+]", "", raw)

    if not s:
        return None

    # 00xx -> +xx
    if s.startswith("00"):
        s = "+" + s[2:]

    # saniraj višak '+'
    if s.count("+") > 1:
        s = re.sub(r"\++", "+", s)
    if "+" in s[1:]:
        s = s[0] + s[1:].replace("+", "")

    # ako počinje '0' i nema '+', dodaj pozivni (ako je definisan)
    if s.startswith("0") and not s.startswith("+") and DEFAULT_COUNTRY_CODE:
        s = DEFAULT_COUNTRY_CODE + s.lstrip("0")

    # minimalno 7 cifara
    digits = re.sub(r"\D", "", s)
    if len(digits) < 7:
        return None

    return s

def classify_kontakt(k):
    """
    Vraća (tip, vrijednost):
    - ("email", email) / ("phone", normalizovan_broj) / ("text", original)
    """
    k = (k or "").strip()
    if not k:
        return ("text", "")
    if is_email(k):
        return ("email", k)
    phone = normalize_phone(k)
    if phone:
        return ("phone", phone)
    return ("text", k)

@app.route("/")
def index():
    sada = now_podgorica()
    dan = sada.weekday()  # 0=pon, 6=ned
    ime_dana = DANI_PUNIM[dan].lower()

    # default prema danu u nedelji
    sv = RADNO_VRIJEME.get(ime_dana)
    if sv is None:
        start, end = None, None
    else:
        start, end = sv["start"], sv["end"]

    # prepiši ako u data.json postoji unos za današnji datum
    posebni = ucitaj_posebne_datume()
    datum_str = sada.strftime("%Y-%m-%d")
    ps = posebni.get(datum_str)

    if isinstance(ps, (list, tuple)) and len(ps) == 2:
        start = ps[0] if ps[0] is not None else None
        end   = ps[1] if ps[1] is not None else None

    # provjera u minutama (robustno na float)
    start_m = to_minutes(start) if start is not None else None
    end_m   = to_minutes(end)   if end   is not None else None

    if start_m is None or end_m is None:
        poruka_html = "Danas je neradni dan."
        poruka_tts  = "Danas je neradni dan."
        status_slika = "close1.png"
    else:
        now_m = sada.hour * 60 + sada.minute
        otvoreno_sad = (start_m <= now_m < end_m)
        if otvoreno_sad:
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

    if request.method == "POST":
        datum = (request.form.get("datum") or "").strip()
        if not datum:
            return redirect(url_for("admin"))

        if "neradni" in request.form:
            start = end = None
        else:
            start = to_int_or_none(request.form.get("start"))
            end   = to_int_or_none(request.form.get("end"))
            if start is None or end is None:
                start = end = None

        posebni[datum] = [start, end]
        sacuvaj_posebne_datume(posebni)
        return redirect(url_for("admin"))

    sortirano = dict(sorted(posebni.items()))
    return render_template("admin.html", posebni=sortirano)

@app.route("/obrisi/<datum>")
def obrisi(datum):
    posebni = ucitaj_posebne_datume()
    if datum in posebni:
        del posebni[datum]
        sacuvaj_posebne_datume(posebni)
    return redirect(url_for("admin"))

@app.route("/posalji_poruku", methods=["POST"])
def posalji_poruku():
    data = request.get_json(force=True, silent=True) or {}
    ime     = (data.get("ime") or "").strip()
    kontakt = (data.get("kontakt") or "").strip()
    poruka  = (data.get("poruka") or "").strip()

    if not poruka:
        return jsonify(ok=False, error="Poruka je obavezna."), 400

    now = now_podgorica()

    # Klasifikacija kontakt polja
    kontakt_tip, kontakt_val = classify_kontakt(kontakt)

    if kontakt_tip == "email":
        kontakt_linija = f"E-mail: {kontakt_val}"
    elif kontakt_tip == "phone":
        kontakt_linija = f"Telefon: {kontakt_val}"
    else:
        kontakt_linija = f"Kontakt: {kontakt_val or '—'}"

    body = (
        f"Ime i prezime: {ime or '—'}\n"
        f"{kontakt_linija}\n\n"
        f"Poruka:\n{poruka}\n\n"
        f"Vrijeme: {now.isoformat()}\n"
        f"IP: {request.remote_addr}\n"
    )

    # 1) Arhiva u CSV
    try:
        newfile = not os.path.exists(CSV_PATH)
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if newfile:
                w.writerow(["datetime", "ime", "kontakt", "ip", "poruka"])
            w.writerow([now.isoformat(), ime, kontakt, request.remote_addr, poruka])
    except Exception as e:
        print(f"CSV write error: {e}", flush=True)
        # nastavljamo na slanje maila

    # 2) Slanje email-a (env varijable)
    user   = os.environ.get("GMAIL_USER")
    app_pw = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")

    if not user or not app_pw:
        # i dalje vrati info za call/mail na frontend
        resp = {"ok": True, "warning": "Mail nije poslat (GMAIL_USER/GMAIL_APP_PASSWORD nisu postavljeni).", "kontakt_tip": kontakt_tip}
        if kontakt_tip == "phone" and kontakt_val:
            resp["telefon_norm"] = kontakt_val
            resp["tel_uri"] = "tel:" + re.sub(r"[^\d+]", "", kontakt_val)
        elif kontakt_tip == "email" and kontakt_val:
            resp["email"] = kontakt_val
        return jsonify(resp), 200

    try:
        msg = EmailMessage()
        msg["From"] = formataddr(("PORUKA SA VRATA", user))  # npr. "PORUKA SA VRATA <dentalabplaner@gmail.com>"
        msg["To"] = "dentalabplaner@gmail.com"
        msg["Subject"] = f"[Kontakt sa sajta] {ime or 'Anonimno'} — {now.strftime('%d.%m.%Y %H:%M')}"
        msg.set_content(body)

        # Reply-To ako je e-mail
        if kontakt_tip == "email" and kontakt_val:
            msg["Reply-To"] = kontakt_val

        # (Opcionalno) telefon u custom headeru
        if kontakt_tip == "phone" and kontakt_val:
            msg["X-Contact-Phone"] = kontakt_val

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as smtp:
            smtp.login(user, app_pw)
            smtp.send_message(msg)

    except Exception as e:
        print(f"Mail error: {e}", flush=True)
        # čak i ako mail padne, vrati info za call/mail na frontend
        resp = {"ok": True, "warning": f"CSV sačuvan, ali slanje maila nije uspjelo: {type(e).__name__}", "kontakt_tip": kontakt_tip}
        if kontakt_tip == "phone" and kontakt_val:
            resp["telefon_norm"] = kontakt_val
            resp["tel_uri"] = "tel:" + re.sub(r"[^\d+]", "", kontakt_val)
        elif kontakt_tip == "email" and kontakt_val:
            resp["email"] = kontakt_val
        return jsonify(resp), 200

    # Uspješno
    resp = {"ok": True, "kontakt_tip": kontakt_tip}
    if kontakt_tip == "phone" and kontakt_val:
        resp["telefon_norm"] = kontakt_val
        resp["tel_uri"] = "tel:" + re.sub(r"[^\d+]", "", kontakt_val)
    elif kontakt_tip == "email" and kontakt_val:
        resp["email"] = kontakt_val
    return jsonify(resp), 200

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
