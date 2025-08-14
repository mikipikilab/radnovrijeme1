from flask import Flask, render_template, request, redirect, url_for, jsonify, send_file, abort, Response
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from email.utils import formataddr

import json, os, re, html, urllib.parse
import smtplib, ssl, csv
from email.message import EmailMessage

# APP MORA BITI DEFINISAN PRIJE SVIH @app.route
app = Flask(__name__, template_folder="templates")

# ---- ICS helperi ----
def _ics_ts(dt_aware):
    # očekuje timezone-aware datetime; u .ics pišemo u UTC
    return dt_aware.strftime("%Y%m%dT%H%M%SZ")

def build_ics(summary, dt_local, duration_min=60, description="", location=""):
    # pretvaramo start/end u UTC radi kompatibilnosti
    dt_start_utc = dt_local.astimezone(ZoneInfo("UTC"))
    dt_end_utc = (dt_local + timedelta(minutes=duration_min)).astimezone(ZoneInfo("UTC"))

    start_s = _ics_ts(dt_start_utc)
    end_s   = _ics_ts(dt_end_utc)
    uid = f"{start_s}-{abs(hash((summary, start_s)))}@dentalab"

    # escape novih redova
    desc = (description or "").replace("\r\n", "\n").replace("\n", "\\n")
    loc  = (location or "").replace("\r\n", "\n").replace("\n", "\\n")
    summ = (summary or "Termin").replace("\n", " ")

    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//Dentalab//Appointment//EN\r\n"
        "CALSCALE:GREGORIAN\r\n"
        "METHOD:PUBLISH\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"DTSTAMP:{start_s}\r\n"
        f"DTSTART:{start_s}\r\n"
        f"DTEND:{end_s}\r\n"
        f"SUMMARY:{summ}\r\n"
        f"DESCRIPTION:{desc}\r\n"
        f"LOCATION:{loc}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

@app.get("/event.ics")
def event_ics():
    title = (request.args.get("title") or "Termin").strip()
    start = (request.args.get("start") or "").strip()  # "YYYY-MM-DD HH:MM"
    duration = int((request.args.get("dur") or "60").strip())
    details = (request.args.get("details") or "").strip()
    location = (request.args.get("loc") or "").strip()

    try:
        dt_local = datetime.strptime(start, "%Y-%m-%d %H:%M").replace(
            tzinfo=ZoneInfo("Europe/Podgorica")
        )
    except Exception:
        return "Bad start", 400

    ics_text = build_ics(
        summary=title,
        dt_local=dt_local,
        duration_min=duration,
        description=details,
        location=location,
    )
    return Response(
        ics_text,
        mimetype="text/calendar",
        headers={"Content-Disposition": 'attachment; filename="termin.ics"'}
    )


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
    data = request.get_json(force=True, silent=True) or {}
    ime     = (data.get("ime") or "").strip()
    kontakt = (data.get("kontakt") or "").strip()
    poruka  = (data.get("poruka") or "").strip()

    if not poruka:
        return jsonify(ok=False, error="Poruka je obavezna."), 400

    now = now_podgorica()
    kontakt_tip, kontakt_val = classify_kontakt(kontakt)

    # linije za plain text
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

    body_txt = (
        f"Ime i prezime: {ime or '—'}\n"
        f"{kontakt_linija_txt}\n\n"
        f"Poruka:\n{poruka}\n\n"
        f"Vrijeme: {now.isoformat()}\n"
        f"IP: {request.remote_addr or ''}\n"
    )

    # link za prefill potvrde termina (nije .ics ovdje)
    url_base = request.url_root.rstrip("/")
    confirm_qs = urllib.parse.urlencode({
        "ime": ime or "",
        "email": kontakt_val if (kontakt_tip == "email") else "",
        "telefon": kontakt_val if (kontakt_tip == "phone") else "",
        "ref": f"Ref: poruka sa sajta {now.strftime('%d.%m.%Y %H:%M')}"
    })
    confirm_url = f"{url_base}/potvrdi_termin?{confirm_qs}"
    confirm_btn_html = (
        f'<a href="{html.escape(confirm_url)}" '
        'style="display:inline-block;background:#111827;color:#fff;'
        'padding:12px 18px;border-radius:8px;text-decoration:none;font-weight:700;">'
        'Potvrdi termin</a>'
    )

    # brzi odgovori – ako nemamo e-mail korisnika, šalji na tvoj inbox
    mail_to = "dentalabplaner@gmail.com"
    quick_reply_to = kontakt_val if (kontakt_tip == "email" and kontakt_val) else mail_to

    def build_mailto(to_email: str, subject: str, body: str) -> str:
        qs = {"subject": subject, "body": body}
        return f"mailto:{to_email}?{urllib.parse.urlencode(qs)}"

    hvala_link = build_mailto(
        quick_reply_to,
        f"Hvala na poruci – {ime or 'poštovani/na'}",
        "Hvala Vam na javljanju. Uskoro ćemo se povratno javiti.\n\n— DENTALAB"
    )
    prosledjujem_link = build_mailto(
        quick_reply_to,
        f"Vaša poruka je prosleđena – {ime or 'poštovani/na'}",
        "Vašu poruku smo prosledili nadležnom timu/doktoru. Javićemo Vam se čim dobijemo povratnu informaciju.\n\n— DENTALAB"
    )

    # HTML mail – bez ics_url/when_txt/email/telefon_norm (to je u /potvrdi_termin)
    body_html = f"""
    <html><body style="font-family:Arial,Helvetica,sans-serif; font-size:14px; color:#111;">
      <p><b>Ime i prezime:</b> {html.escape(ime or '—')}</p>
      <p><b>Kontakt:</b> {kontakt_link_html}</p>
      <p><b>Poruka:</b><br>{html.escape(poruka).replace('\\n','<br>')}</p>
      <hr style="border:none;border-top:1px solid #ddd;margin:12px 0">
      <p style="color:#555;">
        Vrijeme: {html.escape(now.isoformat())}<br>
        IP: {html.escape(request.remote_addr or '')}
      </p>
      <div style="margin:20px 0;text-align:center;">
        {confirm_btn_html}
      </div>
      <p style="margin-top:20px;color:#555;">Brzi odgovori:</p>
      <ul style="list-style:none;padding:0;margin:0;">
        <li style="margin:6px 0;"><a href="{html.escape(hvala_link)}" style="color:#2563eb;text-decoration:none;">Hvala</a></li>
        <li style="margin:6px 0;"><a href="{html.escape(prosledjujem_link)}" style="color:#2563eb;text-decoration:none;">Prosleđujem</a></li>
      </ul>
    </body></html>
    """

    # CSV zapis (ISPRAVNA UVLAKA!)
    try:
        newfile = not os.path.exists(CSV_PATH)
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if newfile:
                w.writerow(["datetime", "ime", "kontakt", "ip", "poruka"])
            w.writerow([now.isoformat(), ime, kontakt, request.remote_addr or "", poruka])
    except Exception as e:
        print(f"CSV write error: {e}", flush=True)

    # Slanje e-maila
    user   = os.environ.get("GMAIL_USER")
    app_pw = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    if not user or not app_pw:
        return jsonify(ok=True, warning="Mail nije poslat (GMAIL_USER/GMAIL_APP_PASSWORD nisu postavljeni)."), 200

    try:
        msg = EmailMessage()
        msg["From"] = formataddr(("PORUKA SA SAJTA", user))
        msg["To"] = "dentalabplaner@gmail.com"
        msg["Subject"] = f"[Kontakt sa sajta] {ime or 'Anonimno'} — {now.strftime('%d.%m.%Y %H:%M')}"
        msg.set_content(body_txt)
        msg.add_alternative(body_html, subtype="html")

        if kontakt_tip == "email" and kontakt_val:
            msg["Reply-To"] = kontakt_val
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

@app.route("/potvrdi_termin", methods=["GET", "POST"])
def potvrdi_termin():
    # GET: forma sa flatpickr
    if request.method == "GET":
        ime = (request.args.get("ime") or "").strip()
        email = (request.args.get("email") or "").strip()
        telefon = (request.args.get("telefon") or "").strip()
        ref = (request.args.get("ref") or "").strip()  # napomena/ref

        html_form = f"""
<!DOCTYPE html>
<html lang="sr">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width,initial-scale=1" />
  <title>Potvrda termina</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/flatpickr/dist/flatpickr.min.css">
  <style>
    body {{ font-family: system-ui, Arial, sans-serif; background:#f9fafb; color:#111; margin:0; padding:24px; }}
    .card {{ max-width:520px; margin:0 auto; background:#fff; border:1px solid #e5e7eb; border-radius:12px; padding:18px; box-shadow:0 6px 18px rgba(0,0,0,.06); }}
    h1 {{ font-size:20px; margin:0 0 12px; }}
    .row {{ margin-bottom:10px; }}
    .input {{ width:100%; padding:12px; border:1px solid #d1d5db; border-radius:10px; }}
    .btn {{ display:inline-block; margin-top:8px; padding:12px 18px; border-radius:10px; border:1px solid #d1d5db; background:#111827; color:#fff; font-weight:700; cursor:pointer; }}
    .muted {{ color:#6b7280; font-size:12px; }}
  </style>
</head>
<body>
  <div class="card">
    <h1>Potvrda termina</h1>
    <form method="POST">
      <div class="row">
        <label>Ime i prezime</label>
        <input class="input" name="ime" value="{html.escape(ime)}" placeholder="Ime i prezime" />
      </div>
      <div class="row">
        <label>E-pošta (opciono)</label>
        <input class="input" name="email" value="{html.escape(email)}" placeholder="npr. osoba@mail.com" />
      </div>
      <div class="row">
        <label>Telefon (opciono)</label>
        <input class="input" name="telefon" value="{html.escape(telefon)}" placeholder="+382..." />
      </div>
      <div class="row">
        <label>Datum i vrijeme</label>
        <input id="dt" class="input" name="dt" required placeholder="Izaberite datum i vrijeme" />
      </div>
      <div class="row">
        <label>Napomena (opciono)</label>
        <textarea class="input" name="napomena" rows="3" placeholder="Dodatna napomena...">{html.escape(ref)}</textarea>
      </div>
      <button class="btn" type="submit">Potvrdi termin</button>
      <div class="muted">Zona vremena: Europe/Podgorica</div>
    </form>
  </div>

  <script src="https://cdn.jsdelivr.net/npm/flatpickr"></script>
  <script src="https://cdn.jsdelivr.net/npm/flatpickr/dist/l10n/sr.js"></script>
  <script>
    flatpickr("#dt", {{
      enableTime: true,
      dateFormat: "Y-m-d H:i",
      minDate: "today",
      time_24hr: true,
      locale: "sr"
    }});
  </script>
</body>
</html>
"""
        return html_form

    # POST: obrada + e-mail sa .ics linkom i prilogom
    ime = (request.form.get("ime") or "").strip()
    email = (request.form.get("email") or "").strip()
    telefon_raw = (request.form.get("telefon") or "").strip()
    napomena = (request.form.get("napomena") or "").strip()
    dt_str = (request.form.get("dt") or "").strip()

    # validacija datuma
    try:
        dt_local = datetime.strptime(dt_str, "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Europe/Podgorica"))
    except Exception:
        return "Neispravan datum/vrijeme.", 400

    # priprema podataka
    duration_min = 60  # po želji promijeni trajanje
    telefon_norm = normalize_phone(telefon_raw) or telefon_raw
    when_txt = dt_local.strftime("%d.%m.%Y u %H:%M")
    # URL za .ics (koristi tvoju /event.ics rutu)
    ics_qs = urllib.parse.urlencode({
        "title": f"Termin — {ime or 'Pacijent'}",
        "start": dt_local.strftime("%Y-%m-%d %H:%M"),
        "dur": duration_min,
        "details": napomena or "",
        "loc": "Dentalab, Podgorica"
    })
    ics_url = request.url_root.rstrip("/") + "/event.ics?" + ics_qs
# Google Calendar link
from datetime import timedelta
import urllib.parse

    # --- Google Calendar link (koristi UTC i format YYYYMMDDTHHMMSSZ/...) ---
    start_utc = dt_local.astimezone(ZoneInfo("UTC"))
    end_utc = (dt_local + timedelta(minutes=duration_min)).astimezone(ZoneInfo("UTC"))

    gcal_qs = urllib.parse.urlencode({
        "action": "TEMPLATE",
        "text": f"Termin — {ime or 'Pacijent'}",
        "dates": f"{start_utc.strftime('%Y%m%dT%H%M%SZ')}/{end_utc.strftime('%Y%m%dT%H%M%SZ')}",
        "details": napomena or "",
        "location": "Dentalab, Podgorica",
        "ctz": "Europe/Podgorica"
    })
    google_url = f"https://calendar.google.com/calendar/render?{gcal_qs}"

    # plain tekst
    body_txt = (
        "Termin kod stomatologa\n\n"
        f"Ime i prezime: {ime or '—'}\n"
        f"E-pošta: {email or '—'}\n"
        f"Telefon: {telefon_norm or '—'}\n"
        f"Termin: {when_txt} (Europe/Podgorica)\n"
        f"Napomena: {napomena or '—'}\n\n"
        f"Dodaj u kalendar (.ics): {ics_url}\n"
        f"Dodaj u Google Kalendar: {google_url}\n"
    )

    # HTML (sa dugmadima za .ics i Google)
    body_html = f"""
    <html><body style="font-family:Arial,Helvetica,sans-serif; font-size:14px; color:#111;">
      <h2 style="margin:0 0 8px;">Termin kod stomatologa</h2>
      <p><b>Ime i prezime:</b> {html.escape(ime or '—')}</p>
      <p><b>E-pošta:</b> {html.escape(email or '—')}</p>
      <p><b>Telefon:</b> {html.escape(telefon_norm or '—')}</p>
      <p><b>Termin:</b> {html.escape(when_txt)} <span style="color:#6b7280;">(Europe/Podgorica)</span></p>
      <p><b>Napomena:</b><br>{html.escape(napomena or '—').replace('\\n','<br>')}</p>

      <p style="margin:12px 0;">
        <a href="{html.escape(ics_url)}" style="display:inline-block;background:#111827;color:#fff;
           padding:10px 14px;border-radius:8px;text-decoration:none;font-weight:700;">
           Dodaj u kalendar (.ics)
        </a>
      </p>

      <p style="margin:12px 0;">
        <a href="{html.escape(google_url)}" style="display:inline-block;background:#1a73e8;color:#fff;
           padding:10px 14px;border-radius:8px;text-decoration:none;font-weight:700;">
           Dodaj u Google Kalendar
        </a>
      </p>
    </body></html>
    """

    # priprema i slanje maila (+ priložimo .ics za bolju kompatibilnost)
    user   = os.environ.get("GMAIL_USER")
    app_pw = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    if not user or not app_pw:
        return "Mail nije konfigurisan (GMAIL_USER/GMAIL_APP_PASSWORD).", 200

    # generiši .ics tekst lokalno (koristi tvoju build_ics funkciju)
    ics_text = build_ics(
        summary=f"Termin — {ime or 'Pacijent'}",
        dt_local=dt_local,
        duration_min=duration_min,
        description=napomena or "",
        location="Dentalab, Podgorica"
    )

    try:
        msg = EmailMessage()
        msg["From"] = formataddr(("POTVRDA TERMINA", user))
        msg["To"] = "dentalabplaner@gmail.com"
        if email:
            msg["Cc"] = email
            msg["Reply-To"] = email
        msg["Subject"] = f"{ime or 'Pacijent'}, Vaš termin kod stomatologa je zakazan — {when_txt}"

        msg.set_content(body_txt)
        msg.add_alternative(body_html, subtype="html")

        # .ics kao prilog
        msg.add_attachment(
            ics_text.encode("utf-8"),
            maintype="text",
            subtype="calendar",
            filename="termin.ics"
        )

        context = ssl.create_default_context()
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=context) as smtp:
            smtp.login(user, app_pw)
            smtp.send_message(msg)
    except Exception as e:
        print(f"Mail error (potvrdi_termin): {e}", flush=True)
        return "Greška pri slanju e-pošte.", 500

    return "Termin je potvrđen. Hvala!", 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5098))
    app.run(host="0.0.0.0", port=port, debug=True)
