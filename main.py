import os
import re
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup

# ─────────────────────────────────────────────
# SECRETS (sættes som GitHub Secrets i repo)
# ─────────────────────────────────────────────
TENANT_ID         = os.environ.get("TENANT_ID", "").strip()
CLIENT_ID         = os.environ.get("CLIENT_ID", "").strip()
CLIENT_SECRET     = os.environ.get("CLIENT_SECRET", "").strip()
SENDER_UPN        = os.environ.get("SENDER_UPN", "").strip()
RECIPIENT_1       = os.environ.get("RECIPIENT_1", "").strip()
RECIPIENT_2       = os.environ.get("RECIPIENT_2", "").strip()
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

# ─────────────────────────────────────────────
# INDSTILLINGER
# ─────────────────────────────────────────────
LOOKBACK_DAYS = 7       # Kig 7 dage tilbage
TIMEOUT       = 30      # Netværks-timeout i sekunder
STATE_FILE    = "state.json"

# ─────────────────────────────────────────────
# HØRINGSPORTALEN — ATOM-FEEDS PR. MYNDIGHED
# ─────────────────────────────────────────────
# Authority-IDs fra hoeringsportalen.dk/Syndication
HOERINGSPORTALEN_FEEDS = [
    ("Energistyrelsen",                           655),
    ("Forsyningstilsynet",                        665),
    ("Klima-, Energi- og Forsyningsministeriet", 1742),
    ("Miljøstyrelsen",                            634),
    ("Ministeriet for Samfundssikkerhed og Beredskab", 1779),
    ("Naturstyrelsen",                            702),
    ("Styrelsen for Grøn Arealomlægning og Vandmiljø", 1744),
]

HOERINGSPORTALEN_BASE = (
    "https://hoeringsportalen.dk/Syndication/HearingsByAuthorityFeed?authorityId={}"
)

# ─────────────────────────────────────────────
# NYHEDSSIDER — HTML-SCRAPING
# ─────────────────────────────────────────────
NEWS_SOURCES = [
    {
        "id":    "ens",
        "navn":  "Energistyrelsen",
        "url":   "https://ens.dk/presse/nyheder-og-pressemeddelelser",
        "farve": "#1A5276",
    },
    {
        "id":    "fsts",
        "navn":  "Forsyningstilsynet",
        "url":   "https://forsyningstilsynet.dk/nyheder",
        "farve": "#1A5276",
    },
    {
        "id":    "kefm",
        "navn":  "Klima-, Energi- og Forsyningsministeriet",
        "url":   "https://www.kefm.dk/aktuelt/nyheder",
        "farve": "#1A5276",
    },
    {
        "id":    "mst",
        "navn":  "Miljøstyrelsen",
        "url":   "https://mst.dk/aktuelt/nyheder",
        "farve": "#1A5276",
    },
]

# Fælles browser-headers til HTML-scraping
SCRAPE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "da-DK,da;q=0.9,en;q=0.8",
}


# ─────────────────────────────────────────────
# STATE (dedup)
# ─────────────────────────────────────────────

def load_state():
    """Returnerer set af allerede sete URL'er."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("seen", []))
    except FileNotFoundError:
        print("  ℹ️  state.json ikke fundet — første kørsel")
        return set()
    except Exception as e:
        print(f"  ⚠️  Kunne ikke læse state.json: {e}")
        return set()


def save_state(seen_set):
    """Gemmer state til state.json. Committes automatisk af weekly.yml."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "seen":         sorted(seen_set),
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                },
                f, indent=2, ensure_ascii=False,
            )
        print(f"  ✅ state.json gemt ({len(seen_set)} sete)")
    except Exception as e:
        print(f"  ⚠️  Kunne ikke gemme state.json: {e}")


# ─────────────────────────────────────────────
# MICROSOFT GRAPH — TOKEN OG AFSENDELSE
# ─────────────────────────────────────────────

def get_token():
    """Henter OAuth2-token via client credentials flow."""
    url  = f"https://login.microsoftonline.com/{TENANT_ID}/oauth2/v2.0/token"
    data = {
        "client_id":     CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type":    "client_credentials",
        "scope":         "https://graph.microsoft.com/.default",
    }
    r = requests.post(url, data=data, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()["access_token"]


def send_mail(access_token, subject, html_body):
    """Sender mail via Microsoft Graph /sendMail til de to modtagere."""
    if not SENDER_UPN:
        raise RuntimeError("SENDER_UPN er tom — GitHub secret mangler.")
    url     = f"https://graph.microsoft.com/v1.0/users/{SENDER_UPN}/sendMail"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type":  "application/json",
    }
    payload = {
        "message": {
            "subject": subject,
            "body": {"contentType": "HTML", "content": html_body},
            "toRecipients": [
                {"emailAddress": {"address": RECIPIENT_1}},
                {"emailAddress": {"address": RECIPIENT_2}},
            ],
        },
        "saveToSentItems": True,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    print("✅ Mail sendt.")


# ─────────────────────────────────────────────
# CLAUDE — BESKRIVELSE AF NYHED
# ─────────────────────────────────────────────

def claude_beskriv(titel, kilde, url):
    """
    Beder Claude skrive en neutral 2-3 linjers beskrivelse af nyheden
    baseret på titel og kilde. Returnerer beskrivelsestekst eller None.
    """
    if not ANTHROPIC_API_KEY:
        return None

    prompt = f"""Du er juridisk assistent for et dansk advokatfirma specialiseret i
energi- og forsyningsret, miljøret og regulatorisk ret.

Skriv en kort, neutral og præcis beskrivelse (2-3 sætninger) af følgende nyhed
fra en dansk myndighed. Beskriv kun hvad titlen angiver — ingen fortolkning.
Sproget er sagligt og egnet til professionel brug.

Kilde: {kilde}
Titel: {titel}
URL: {url}

Svar KUN med beskrivelsesteksten — ingen overskrift, ingen JSON, ingen præambel."""

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model":      "claude-haiku-4-5",
                "max_tokens": 200,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=TIMEOUT,
        )
        if r.status_code == 429:
            print("  ⏳ Rate limit — venter 10 sek...")
            time.sleep(10)
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Content-Type":      "application/json",
                    "x-api-key":         ANTHROPIC_API_KEY,
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model":      "claude-haiku-4-5",
                    "max_tokens": 200,
                    "messages":   [{"role": "user", "content": prompt}],
                },
                timeout=TIMEOUT,
            )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()
    except Exception as e:
        print(f"  ⚠️  Claude-kald fejlede for '{titel[:40]}': {e}")
        return None


# ─────────────────────────────────────────────
# HØRINGSPORTALEN — ATOM-FEEDS
# ─────────────────────────────────────────────

def fetch_hearings(seen):
    """
    Henter aktive høringer fra Høringsportalen via ATOM-feeds.
    Returnerer liste af dicts med: titel, url, myndighed, type, frist, dato.
    Filtrerer på LOOKBACK_DAYS og dedup mod seen.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    results = []
    seen_urls = set()

    for myndighed, authority_id in HOERINGSPORTALEN_FEEDS:
        feed_url = HOERINGSPORTALEN_BASE.format(authority_id)
        try:
            r = requests.get(feed_url, headers=SCRAPE_HEADERS, timeout=TIMEOUT)
            if r.status_code != 200:
                print(f"  ⚠️  Høringsportalen ({myndighed}): HTTP {r.status_code}")
                continue

            root = ET.fromstring(r.content)
            ns   = {"atom": "http://www.w3.org/2005/Atom"}

            entries = root.findall("atom:entry", ns)
            print(f"  → Høringsportalen / {myndighed}: {len(entries)} entries")

            for entry in entries:
                titel   = (entry.findtext("atom:title", "", ns) or "").strip()
                url     = ""
                for link in entry.findall("atom:link", ns):
                    if link.get("rel", "alternate") == "alternate":
                        url = link.get("href", "")
                        break
                if not url:
                    link_el = entry.find("atom:link", ns)
                    if link_el is not None:
                        url = link_el.get("href", "")

                updated_str = entry.findtext("atom:updated", "", ns) or ""
                published_str = entry.findtext("atom:published", "", ns) or ""
                dato_str = updated_str or published_str

                try:
                    dato = datetime.fromisoformat(dato_str.replace("Z", "+00:00"))
                except ValueError:
                    dato = datetime.now(timezone.utc)

                if dato < cutoff:
                    continue
                if not url or url in seen or url in seen_urls:
                    continue

                # Udpak ekstra metadata fra summary
                summary = entry.findtext("atom:summary", "", ns) or ""
                summary_clean = re.sub(r"<[^>]+>", " ", summary)
                summary_clean = re.sub(r"\s+", " ", summary_clean).strip()

                # Forsøg at udpak type og frist fra summary
                hoering_type = ""
                frist        = "ikke angivet"

                type_match  = re.search(r"Type:\s*([^\n|]+)", summary_clean)
                frist_match = re.search(r"Høringsfrist:\s*(\d{2}-\d{2}-\d{4})", summary_clean)
                if type_match:
                    hoering_type = type_match.group(1).strip()
                if frist_match:
                    frist = frist_match.group(1)

                seen_urls.add(url)
                results.append({
                    "kilde":    "Høringsportalen",
                    "myndighed": myndighed,
                    "titel":    titel,
                    "url":      url,
                    "dato":     dato,
                    "type":     hoering_type,
                    "frist":    frist,
                    "summary":  summary_clean[:300],
                })

        except ET.ParseError as e:
            print(f"  ⚠️  Høringsportalen ({myndighed}): XML-parse fejl: {e}")
        except Exception as e:
            print(f"  ⚠️  Høringsportalen ({myndighed}): {type(e).__name__}: {e}")

    print(f"  → Høringsportalen total: {len(results)} nye høringer")
    return results


# ─────────────────────────────────────────────
# NYHEDSSIDER — HTML-SCRAPING
# ─────────────────────────────────────────────

def _parse_danish_date(text):
    """
    Forsøger at parse datoer i danske formater:
      "12. juni 2026", "12-06-2026", "2026-06-12", "12/06/2026"
    Returnerer datetime (UTC) eller None.
    """
    MAANEDER = {
        "januar": 1, "februar": 2, "marts": 3, "april": 4,
        "maj": 5, "juni": 6, "juli": 7, "august": 8,
        "september": 9, "oktober": 10, "november": 11, "december": 12,
    }
    text = text.strip().lower()

    # "12. juni 2026"
    m = re.search(r"(\d{1,2})\.\s*([a-zæøå]+)\s+(\d{4})", text)
    if m:
        dag, maaned_navn, aar = m.group(1), m.group(2), m.group(3)
        maaned = MAANEDER.get(maaned_navn)
        if maaned:
            return datetime(int(aar), maaned, int(dag), tzinfo=timezone.utc)

    # ISO "2026-06-12"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        tzinfo=timezone.utc)

    # "12-06-2026" eller "12/06/2026"
    m = re.search(r"(\d{1,2})[-/](\d{2})[-/](\d{4})", text)
    if m:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)),
                        tzinfo=timezone.utc)

    return None


def _find_articles(soup, base_url):
    """
    Generisk artikel-finder der virker på tværs af de fleste danske
    myndighedssites (SiteCore/Umbraco-baserede).
    Returnerer liste af (titel, url, dato_tekst).
    """
    candidates = []

    # Strategi 1: <article>-elementer
    for art in soup.find_all("article"):
        a_tag = art.find("a", href=True)
        if not a_tag:
            continue
        titel = a_tag.get_text(strip=True) or art.find(
            ["h1", "h2", "h3", "h4"]
        )
        if hasattr(titel, "get_text"):
            titel = titel.get_text(strip=True)
        url   = a_tag["href"]
        dato_el = art.find(["time", "span", "p"],
                           class_=re.compile(r"date|dato|time|publish", re.I))
        dato_text = dato_el.get_text(strip=True) if dato_el else ""
        if titel:
            candidates.append((titel, url, dato_text))

    if candidates:
        return candidates

    # Strategi 2: elementer med news/nyhed/press i class
    for el in soup.find_all(
        ["li", "div"],
        class_=re.compile(r"news|nyhed|press|article|item", re.I),
    ):
        a_tag = el.find("a", href=True)
        heading = el.find(["h2", "h3", "h4", "strong"])
        if not a_tag:
            continue
        titel = (heading.get_text(strip=True) if heading
                 else a_tag.get_text(strip=True))
        url   = a_tag["href"]
        dato_el = el.find(["time", "span"],
                          class_=re.compile(r"date|dato|time|publish", re.I))
        dato_text = dato_el.get_text(strip=True) if dato_el else ""
        if titel:
            candidates.append((titel, url, dato_text))

    if candidates:
        return candidates

    # Strategi 3: Alle <h2>/<h3> med link
    for heading in soup.find_all(["h2", "h3", "h4"]):
        a_tag = heading.find("a", href=True) or (
            heading.find_parent(["li", "div"]) or heading
        ).find("a", href=True) if heading.find_parent(["li", "div"]) else None
        if not a_tag:
            continue
        titel = heading.get_text(strip=True)
        url   = a_tag["href"]
        if titel:
            candidates.append((titel, url, ""))

    return candidates


def fetch_news_source(source, seen):
    """
    Scraper én nyhedskilde og returnerer liste af nye nyheder.
    Hvert element: { id, navn, titel, url, dato, farve }
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    results = []

    try:
        r = requests.get(source["url"], headers=SCRAPE_HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"  ⚠️  {source['navn']}: HTTP {r.status_code}")
            return results

        soup  = BeautifulSoup(r.text, "html.parser")
        items = _find_articles(soup, source["url"])
        print(f"  → {source['navn']}: {len(items)} artikler fundet på siden")

        seen_urls = set()
        for titel, href, dato_text in items:
            # Normalisér URL
            if href.startswith("http"):
                full_url = href
            elif href.startswith("/"):
                from urllib.parse import urlparse
                parsed = urlparse(source["url"])
                full_url = f"{parsed.scheme}://{parsed.netloc}{href}"
            else:
                full_url = source["url"].rstrip("/") + "/" + href

            if full_url in seen or full_url in seen_urls:
                continue

            # Forsøg datoparse — spring over hvis ældre end cutoff
            dato = _parse_danish_date(dato_text) if dato_text else None
            if dato and dato < cutoff:
                continue

            seen_urls.add(full_url)
            results.append({
                "kilde_id": source["id"],
                "navn":     source["navn"],
                "titel":    titel,
                "url":      full_url,
                "dato":     dato,
                "farve":    source["farve"],
            })

    except Exception as e:
        print(f"  ⚠️  {source['navn']}: {type(e).__name__}: {e}")

    print(f"  → {source['navn']}: {len(results)} nye nyheder efter dedup")
    return results


# ─────────────────────────────────────────────
# HTML-BUILDER
# ─────────────────────────────────────────────

# Hulgaard brand-farver (genbrugt fra Pipeline 1)
BRAND_PRIMARY    = "#9BC4E2"   # pale cerulean
BRAND_BG         = "#DDE9F3"
BRAND_DARK       = "#14143C"   # navy
SECTION_ENERGI   = "#1A5276"   # mørkeblå — energi/forsyning
SECTION_HOERING  = "#145A32"   # mørkegrøn — høringer

HULGAARD_LOGO_URL = (
    "https://hulgaardadvokater.dk/wp-content/uploads/2022/03/logo.png"
)


def _format_dato(dato):
    """Returnerer dansk datostreng, fx '12. juni 2026'."""
    if not dato:
        return ""
    MAANEDER = [
        "", "januar", "februar", "marts", "april", "maj", "juni",
        "juli", "august", "september", "oktober", "november", "december",
    ]
    return f"{dato.day}. {MAANEDER[dato.month]} {dato.year}"


def _nyhed_html(item):
    """Returnerer HTML-blok for én nyhed (nyhedskilder)."""
    dato_str = _format_dato(item.get("dato"))
    dato_html = (
        f'<p style="margin:0 0 4px 0;font-size:11px;color:#888;">{dato_str}</p>'
        if dato_str else ""
    )
    beskrivelse = item.get("beskrivelse", "")
    besk_html = (
        f'<p style="margin:6px 0 0 0;font-size:13px;color:#333;">{beskrivelse}</p>'
        if beskrivelse else ""
    )
    return f"""
<table width="100%" cellpadding="0" cellspacing="0" border="0"
       style="margin-bottom:10px;border-left:3px solid {item['farve']};
              background:#F4F6F8;border-radius:0;">
  <tr>
    <td style="padding:10px 14px;">
      {dato_html}
      <p style="margin:0;font-size:14px;font-weight:bold;">
        <a href="{item['url']}" style="color:{BRAND_DARK};text-decoration:none;">
          {item['titel']}
        </a>
      </p>
      {besk_html}
    </td>
  </tr>
</table>"""


def _hoering_html(item):
    """Returnerer HTML-blok for én høring."""
    dato_str = _format_dato(item.get("dato"))
    frist    = item.get("frist", "ikke angivet")
    myndigh  = item.get("myndighed", "")
    h_type   = item.get("type", "")

    meta_dele = []
    if myndighed := myndigh:
        meta_dele.append(myndighed)
    if h_type:
        meta_dele.append(h_type)
    if dato_str:
        meta_dele.append(f"Publiceret: {dato_str}")
    meta_html = " · ".join(meta_dele)

    return f"""
<table width="100%" cellpadding="0" cellspacing="0" border="0"
       style="margin-bottom:10px;border-left:3px solid {SECTION_HOERING};
              background:#F4F6F8;border-radius:0;">
  <tr>
    <td style="padding:10px 14px;">
      <p style="margin:0 0 3px 0;font-size:11px;color:#888;">{meta_html}</p>
      <p style="margin:0;font-size:14px;font-weight:bold;">
        <a href="{item['url']}" style="color:{BRAND_DARK};text-decoration:none;">
          {item['titel']}
        </a>
      </p>
      <p style="margin:5px 0 0 0;font-size:12px;color:#555;">
        <strong>Høringsfrist:</strong> {frist}
      </p>
    </td>
  </tr>
</table>"""


def _sektion_html(overskrift, farve, indhold_html):
    """Returnerer HTML-blok for én sektion med overskrift og indhold."""
    return f"""
<table width="100%" cellpadding="0" cellspacing="0" border="0"
       style="margin-bottom:20px;">
  <tr>
    <td style="padding:8px 14px;background:{farve};">
      <p style="margin:0;font-size:15px;font-weight:bold;color:#ffffff;
                font-family:Aptos,Calibri,Arial,sans-serif;">
        {overskrift}
      </p>
    </td>
  </tr>
  <tr>
    <td style="padding:12px 0 0 0;">
      {indhold_html}
    </td>
  </tr>
</table>"""


def build_html(news_by_source, hearings):
    """
    Bygger den fulde HTML-e-mail.
    news_by_source: dict  kilde_id → liste af nyheds-dicts
    hearings:       liste af høring-dicts
    """
    now      = datetime.now(timezone.utc)
    dato_str = _format_dato(now)

    total_nyheder  = sum(len(v) for v in news_by_source.values())
    total_horinger = len(hearings)
    total          = total_nyheder + total_horinger

    subject = (
        f"Energi & Forsyning — ugentlig opdatering {dato_str} "
        f"({total} {'nyhed' if total == 1 else 'nyheder'})"
    )

    # Byg sektioner
    sektioner_html = ""

    # Nyhedskilder
    for source in NEWS_SOURCES:
        items = news_by_source.get(source["id"], [])
        if not items:
            continue
        blokke = "".join(_nyhed_html(it) for it in items)
        sektioner_html += _sektion_html(source["navn"], SECTION_ENERGI, blokke)

    # Høringer
    if hearings:
        blokke = "".join(_hoering_html(it) for it in hearings)
        sektioner_html += _sektion_html(
            f"Høringer ({len(hearings)})", SECTION_HOERING, blokke
        )

    if not sektioner_html:
        sektioner_html = """
<table width="100%" cellpadding="0" cellspacing="0" border="0">
  <tr>
    <td style="padding:20px;text-align:center;color:#888;font-size:14px;">
      Ingen nye nyheder eller høringer denne uge.
    </td>
  </tr>
</table>"""

    html = f"""<!DOCTYPE html>
<html lang="da">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:{BRAND_BG};">
<table width="100%" cellpadding="0" cellspacing="0" border="0"
       style="background:{BRAND_BG};font-family:Aptos,Calibri,Arial,sans-serif;">
  <tr>
    <td align="center" style="padding:20px 10px;">

      <!-- WRAPPER -->
      <table width="580" cellpadding="0" cellspacing="0" border="0"
             style="max-width:580px;background:#ffffff;">

        <!-- HEADER -->
        <tr>
          <td style="background:{BRAND_PRIMARY};padding:18px 24px;">
            <table width="100%" cellpadding="0" cellspacing="0" border="0">
              <tr>
                <td>
                  <img src="{HULGAARD_LOGO_URL}" alt="Hulgaard Advokater"
                       height="38"
                       style="display:block;border:0;height:38px;" />
                </td>
                <td align="right" style="vertical-align:middle;">
                  <p style="margin:0;font-size:12px;color:#14143C;">
                    <strong>Energi &amp; Forsyning</strong><br>
                    Ugentlig opdatering · {dato_str}
                  </p>
                </td>
              </tr>
            </table>
          </td>
        </tr>

        <!-- INTRO-LINJE -->
        <tr>
          <td style="padding:14px 24px 4px 24px;
                     border-bottom:1px solid #e0e0e0;">
            <p style="margin:0;font-size:13px;color:#555;">
              Denne uge: <strong>{total_nyheder} {'nyhed' if total_nyheder == 1 else 'nyheder'}</strong>
              fra myndighederne
              og <strong>{total_horinger} {'høring' if total_horinger == 1 else 'høringer'}</strong>
              på Høringsportalen.
            </p>
          </td>
        </tr>

        <!-- INDHOLD -->
        <tr>
          <td style="padding:18px 24px;">
            {sektioner_html}
          </td>
        </tr>

        <!-- FOOTER -->
        <tr>
          <td style="background:#F4F4F4;padding:12px 24px;
                     border-top:1px solid #e0e0e0;">
            <p style="margin:0;font-size:11px;color:#999;text-align:center;">
              Hulgaard Advokater P/S · Energi &amp; Forsyning ·
              Automatisk genereret {dato_str} ·
              Kilder: Energistyrelsen, Forsyningstilsynet, KEFM,
              Miljøstyrelsen, Høringsportalen
            </p>
          </td>
        </tr>

      </table>
    </td>
  </tr>
</table>
</body>
</html>"""

    return subject, html


# ─────────────────────────────────────────────
# KØRSEL
# ─────────────────────────────────────────────

if __name__ == "__main__":
    # Valider at alle nødvendige secrets er sat
    missing = [
        k for k, v in {
            "TENANT_ID":     TENANT_ID,
            "CLIENT_ID":     CLIENT_ID,
            "CLIENT_SECRET": CLIENT_SECRET,
            "SENDER_UPN":    SENDER_UPN,
            "RECIPIENT_1":   RECIPIENT_1,
            "RECIPIENT_2":   RECIPIENT_2,
        }.items() if not v
    ]
    if missing:
        raise RuntimeError(f"Manglende GitHub secrets: {', '.join(missing)}")

    if not ANTHROPIC_API_KEY:
        print("ℹ️  ANTHROPIC_API_KEY ikke sat — AI-beskrivelser deaktiveret")

    # ── Indlæs state ──
    print("▶ Indlæser state...")
    seen = load_state()
    print(f"  → {len(seen)} sete URLs")

    # ── Hent høringer fra Høringsportalen ──
    print(f"▶ Henter høringer fra Høringsportalen ({len(HOERINGSPORTALEN_FEEDS)} feeds)...")
    hearings = fetch_hearings(seen)

    # ── Hent nyheder fra de 4 nyhedskilder ──
    news_by_source = {}
    for source in NEWS_SOURCES:
        print(f"▶ Henter nyheder fra {source['navn']}...")
        items = fetch_news_source(source, seen)
        if items:
            news_by_source[source["id"]] = items

    # ── AI-beskrivelser (valgfrit) ──
    if ANTHROPIC_API_KEY:
        total_items = sum(len(v) for v in news_by_source.values())
        print(f"▶ Genererer Claude-beskrivelser for {total_items} nyheder...")
        for source_id, items in news_by_source.items():
            for it in items:
                beskrivelse = claude_beskriv(it["titel"], it["navn"], it["url"])
                if beskrivelse:
                    it["beskrivelse"] = beskrivelse
                time.sleep(0.3)

    # ── Opdatér state ──
    new_urls = set()
    for items in news_by_source.values():
        for it in items:
            new_urls.add(it["url"])
    for it in hearings:
        new_urls.add(it["url"])

    save_state(seen | new_urls)

    # ── Byg HTML ──
    print("▶ Bygger HTML-mail...")
    subject, html = build_html(news_by_source, hearings)

    # ── Hent token og send mail ──
    print("▶ Henter Microsoft Graph-token...")
    token = get_token()

    print(f"▶ Sender mail til {RECIPIENT_1} og {RECIPIENT_2}...")
    send_mail(token, subject, html)
    print("✅ Færdig.")
