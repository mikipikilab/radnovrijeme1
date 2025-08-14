from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, abort
from datetime import datetime
from zoneinfo import ZoneInfo
from email.utils import formataddr

import json, os, re, html, urllib.parse
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

def build_mailto(to_email: str, subject: str, body: str) -> str:
    qs = {"subject": subject, "body": body}
    return f"mailto:{to_email}?{urllib.parse.urlencode(qs)}"

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
    # --- Ulazni podaci ---
    data = request.get_json(force=True, silent=True) or {}
    ime     = (data.get("ime") or "").strip()
    kontakt = (data.get("kontakt") or "").strip()
    poruka  = (data.get("poruka") or "").strip()

    if not poruka:
        return jsonify(ok=False, error="Poruka je obavezna."), 400

    now = now_podgorica()

    # --- Klasifikacija kontakta (email / phone / text) ---
    kontakt_tip, kontakt_val = classify_kontakt(kontakt)

    # kome stiže poruka u startu (inbox ordinacije)
    mail_to = "dentalabplaner@gmail.com"

    # e-mail na koji će ići BRZI ODGOVORI (ako imamo korisnikov e-mail, odgovaramo njemu)
    quick_reply_to = kontakt_val if kontakt_tip == "email" else mail_to

    # --- Kontakt linije (plain + HTML) ---
    if kontakt_tip == "email":
        kontakt_linija_txt = f"E-mail: {kontakt_val}"
        kontakt_link_html  = f'<a href="mailto:{html.escape(kontakt_val)}" style="color:#2563eb;text-decoration:none;">{html.escape(kontakt_val)}</a>'
    elif kontakt_tip == "phone":
        tel_uri = "tel:" + re.sub(r"[^\d+]", "", kontakt_val)
        kontakt_linija_txt = f"Telefon: {kontakt_val}"
        kontakt_link_html  = f'<a href="{html.escape(tel_uri)}" style="color:#2563eb;text-decoration:none;">{html.escape(kontakt_val)}</a>'
    else:
        kontakt_linija_txt = f"Kontakt: {kontakt_val or '—'}"
        kontakt_link_html  = html.escape(kontakt_val or "—")

    # --- PLAIN tekst tijelo (fallback) ---
    body_txt = (
        f"Ime i prezime: {ime or '—'}\n"
        f"{kontakt_linija_txt}\n\n"
        f"Poruka:\n{poruka}\n\n"
        f"Vrijeme: {now.isoformat()}\n"
        f"IP: {request.remote_addr or ''}\n"
    )

    # --- Brzi auto-odgovori (mailto šabloni) ---
    quoted = f"> {poruka.replace('\\n', '\\n> ')}"  # prost quote originala
    quick_templates = [
        (
            "Hvala",
            f"Hvala na poruci – {ime or 'poštovani/na'}",
            f"Poštovani/na {ime or ''},\n\nhvala Vam na javljanju. Uskoro ćemo se povratno javiti.\n\n{quoted}\n\nSrdačno,\nDENTALAB"
        ),
        (
            "Prosleđeno",
            f"Vaša poruka je prosleđena – {ime or 'poštovani/na'}",
            f"Poštovani/na {ime or ''},\n\nvašu poruku smo prosledili nadležnom timu/doktoru. Javićemo Vam se čim dobijemo povratnu informaciju.\n\n{quoted}\n\nSrdačno,\nDENTALAB"
        ),
        (
            "Potvrdi termin",
            f"Potvrda termina – {ime or 'pacijent'}",
            f"Poštovani/na {ime or ''},\n\npotvrđujemo termin.\nDatum: [upišite datum]\nVrijeme: [upišite vrijeme]\nLokacija: [upišite lokaciju]\n\n{quoted}\n\nSrdačno,\nDENTALAB"
        ),
        (
            "Nazvaćemo Vas",
            f"Poziv u najskorije vrijeme – {ime or 'poštovani/na'}",
            f"Poštovani/na {ime or ''},\n\nkontaktiraćemo Vas telefonom u najskorije vrijeme.\n\n{quoted}\n\nSrdačno,\nDENTALAB"
        ),
        (
            "Odloženo",
            f"Dogovor za drugi termin – {ime or 'poštovani/na'}",
            f"Poštovani/na {ime or ''},\n\nhvala na poruci. Molimo da usaglasimo novi termin.\nPredlog: [upišite termin]\n\n{quoted}\n\nSrdačno,\nDENTALAB"
        ),
    ]

    # napravi HTML dugmad
    small_btn = (
        "display:inline-block;background:#f3f4f6;color:#111827;padding:10px 12px;"
        "border-radius:8px;text-decoration:none;font-weight:700;border:1px solid #e5e7eb;"
    )

    quick_buttons_html = "".join(
        f'<td style="padding:6px 6px;"><a href="{html.escape(build_mailto(quick_reply_to, subj, body))}" style="{small_btn}">{html.escape(label)}</a></td>'
        for (label, subj, body) in quick_templates
    )

    # --- HTML tijelo (sa brzim odgovorima) ---
    body_html = f"""
    <html>
    <body style="margin:0;padding:0;background:#ffffff;">
      <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#ffffff;">
        <tr><td align="center">
          <table role="presentation" cellpadding="0" cellspacing="0" width="600" style="max-width:600px;width:100%;font-family:Arial,Helvetica,sans-serif;color:#111827;">
            <tr><td style="padding:20px 20px 10px;">
              <h2 style="margin:0 0 8px 0;font-size:20px;">Nova poruka sa sajta</h2>
              <p style="margin:4px 0;"><b>Ime i prezime:</b> {html.escape(ime or '—')}</p>
              <p style="margin:4px 0;"><b>Kontakt:</b> {kontakt_link_html}</p>
              <p style="margin:12px 0;"><b>Poruka:</b><br>{html.escape(poruka).replace('\\n','<br>')}</p>
              <p style="margin:12px 0;color:#6b7280;font-size:12px;">
                Vrijeme: {html.escape(now.isoformat())}<br>
                IP: {html.escape(request.remote_addr or '')}
              </p>
            </td></tr>

            <tr><td style="padding:0 10px 20px;">
              <table role="presentation" cellpadding="0" cellspacing="0" align="center">
                <tr>{quick_buttons_html}</tr>
              </table>
              <p style="text-align:center;margin:10px 0 0;color:#6b7280;font-size:12px;">
                Brzi odgovori (Gmail/Outlook kompatibilno)
              </p>
            </td></tr>
          </table>
        </td></tr>
      </table>
    </body>
    </html>
    """

    # --- CSV arhiva (best-effort) ---
    try:
        newfile = not os.path.exists(CSV_PATH)
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if newfile:
                w.writerow(["datetime", "ime", "kontakt", "ip", "poruka"])
            w.writerow([now.isoformat(), ime, kontakt, request.remote_addr or "", poruka])
    except Exception as e:
        print(f"CSV write error: {e}", flush=True)

    # --- Slanje e-maila (ako je konfigurisan) ---
    user   = os.environ.get("GMAIL_USER")
    app_pw = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")

    if not user or not app_pw:
        return jsonify(ok=True, warning="Mail nije poslat (GMAIL_USER/GMAIL_APP_PASSWORD nisu postavljeni)."), 200

    try:
        msg = EmailMessage()
        msg["From"] = formataddr(("PORUKA SA SAJTA", user))
        msg["To"] = mail_to
        msg["Subject"] = f"[Kontakt sa sajta] {ime or 'Anonimno'} — {now.strftime('%d.%m.%Y %H:%M')}"
        msg.set_content(body_txt)
        msg.add_alternative(body_html, subtype="html")

        # Reply-To ako je korisnikov e-mail dostavljen
        if kontakt_tip == "email" and kontakt_val:
            msg["Reply-To"] = kontakt_val

        # (opciono) telefon u custom headeru
        if kontakt_tip == "phone" and kontakt_val:
            msg["X-Contact-Phone"] = kontakt_val

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
