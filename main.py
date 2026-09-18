import os
import re
import json
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from email.utils import parsedate_to_datetime

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
RECIPIENT_3       = os.environ.get("RECIPIENT_3", "").strip()
RECIPIENT_4       = os.environ.get("RECIPIENT_4", "").strip()
RECIPIENT_5       = os.environ.get("RECIPIENT_5", "").strip()
RECIPIENT_6       = os.environ.get("RECIPIENT_6", "").strip()
RECIPIENT_7       = os.environ.get("RECIPIENT_7", "").strip()
# Alle modtagere samlet — tilføj nye ved at sætte GitHub Secret + tilføje til listen
RECIPIENTS = [r for r in [
    RECIPIENT_1, RECIPIENT_2, RECIPIENT_3, RECIPIENT_4,
    RECIPIENT_5, RECIPIENT_6, RECIPIENT_7,
] if r]
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()

# ─────────────────────────────────────────────
# INDSTILLINGER
# ─────────────────────────────────────────────
LOOKBACK_DAYS    = 7    # Kig 7 dage tilbage
TIMEOUT          = 30   # Netværks-timeout i sekunder
STATE_FILE       = "state.json"

# ─────────────────────────────────────────────
# HØRINGSPORTALEN — ATOM-FEEDS PR. MYNDIGHED
# ─────────────────────────────────────────────
# Authority-IDs fra hoeringsportalen.dk/Syndication
# Alle relevante myndigheder fra Lisas liste med kendte Høringsportal-IDs.
# Ministeriet for Grøn Trepart og Miljø- og Ligestillingsministeriet er nye
# (oprettet juni 2026) og har endnu ikke feeds på Høringsportalen.
HOERINGSPORTALEN_FEEDS = [
    # Styrelser
    ("Energistyrelsen",                                    655),
    ("Forsyningstilsynet",                                 665),
    ("Miljøstyrelsen",                                     634),
    ("Naturstyrelsen",                                     702),
    ("Styrelsen for Grøn Arealomlægning og Vandmiljø",   1744),
    ("Beredskabsstyrelsen",                                626),
    # Ministerier
    ("Klima-, Energi- og Forsyningsministeriet",          1742),
    ("Ministeriet for Samfundssikkerhed og Beredskab",   1779),
    ("Erhvervsministeriet",                                619),
    ("Forsvarsministeriet",                                605),
    ("Miljøministeriet",                                   610),
]

HOERINGSPORTALEN_BASE = (
    "https://hoeringsportalen.dk/Syndication/HearingsByAuthorityFeed?authorityId={}"
)

# ─────────────────────────────────────────────
# NYHEDSKILDER — KUN RSS
# ─────────────────────────────────────────────
# ENS, FSTS og MST bruger JS-rendering og dækkes i stedet via Høringsportalen.
# KEFM har et officielt RSS-feed der virker pålideligt.
NEWS_SOURCES = [
    {
        "id":    "kefm",
        "navn":  "Klima-, Energi- og Forsyningsministeriet",
        "url":   "https://www.kefm.dk/handlers/DynamicRss.ashx?id=76163fac-6c0a-4edb-8e6e-86a4dcf36bd4",
        "type":  "rss",
        "farve": "#1A5276",
    },
    {
        "id":    "energinet",
        "navn":  "Energinet",
        "url":   "https://via.ritzau.dk/rss/releases/latest?publisherId=10304728",
        "type":  "rss",
        "farve": "#1A5276",
    },
    {
        "id":    "eu_energy",
        "navn":  "EU-Kommissionen (energi)",
        "url":   "https://ec.europa.eu/commission/presscorner/api/rss?language=en&policyarea=19&pagesize=20",
        "type":  "rss",
        "farve": "#003399",
    },
    {
        "id":    "eu_env",
        "navn":  "EU-Kommissionen (miljø og klima)",
        "url":   "https://ec.europa.eu/commission/presscorner/api/rss?language=en&policyarea=22&pagesize=20",
        "type":  "rss",
        "farve": "#003399",
    },
    {
        "id":    "mim",
        "navn":  "Miljøministeriet",
        "url":   "https://via.ritzau.dk/rss/releases/latest?publisherId=13560422",
        "type":  "rss",
        "farve": "#1A5276",
    },
    {
        "id":    "gpd",
        "navn":  "Green Power Denmark",
        "url":   "https://via.ritzau.dk/rss/releases/latest?publisherId=13560944",
        "type":  "rss",
        "farve": "#1A5276",
    },
    {
        "id":    "df",
        "navn":  "Dansk Fjernvarme",
        "url":   "https://via.ritzau.dk/rss/releases/latest?publisherId=3320505",
        "type":  "rss",
        "farve": "#1A5276",
    },
]

# ─────────────────────────────────────────────
# FOLKETINGET — ODA API (lovforslag)
# ─────────────────────────────────────────────
# Officielt åbent JSON-API. typeid=3 = lovforslag (bekræftet fra API).
# Samlings-ID bygges automatisk: år*10+1 for første samling i kalenderåret.
# URL bygges i fetch_lovforslag med dato-filter på opdateringsdato.
FT_ODA_BASE = "https://oda.ft.dk/api/Sag"

# Samlings-ID: sættes som GitHub Secret FT_SAMLING eller hardcodes herunder.
# Format: ÅÅÅÅx — fx 20252 = 2. samling 2025 (ekstraordinær efter valg/skift).
# Opdatér ved ny samling (typisk én gang om året i oktober).
FT_SAMLING = os.environ.get("FT_SAMLING", "20252").strip() or "20252"

# Emneord der indikerer relevans for energi/forsyning/klima/miljø/beredskab.
# Lovforslag der ikke matcher noget af dette filtreres fra.
# Bredt forfilter — fjerner åbenlyst irrelevante lovforslag inden Claude-kald.
# Claude vurderer herefter om de resterende er relevante for energi/forsyning/klima/miljø.
FT_UDELUK_EMNEORD = {
    "udlændinge", "straffeloven", "folkeskolen", "gymnasie",
    "erhvervsuddannelse", "dagpenge", "sygedagpenge", "barsel",
    "pension", "folkepension", "boligstøtte", "kontanthjælp",
    "aktieselskab", "selskabsskat", "moms", "tinglysning",
    "domstol", "retspleje", "politi", "fængsel", "kriminal",
    "sundhedsloven", "sygehus", "læge", "medicin", "apotek",
    "daginstitution", "børnepasning", "folkehøjskole",
    "solarier", "tatovering", "tobak", "alkohol",
    "spil", "lotteri", "dyrevelfærd",
}

# ─────────────────────────────────────────────
# DOMSDATABASEN — DANSKE DOMME OG KENDELSER
# ─────────────────────────────────────────────
DOMS_RSS_URL = (
    "https://domsdatabasen.dk/webapi/api/Case/rss"
    "?Title=Seneste%20domme%20og%20kendelser"
    "&SortingParameter=PublishDate"
    "&DescendingOrder=true"
    "&TimeAmount=7"
    "&TimeType=Days"
)

# Emneord til forfiltrering af domme — samme princip som FT_UDELUK_EMNEORD
# men her er det positivt: kun domme der matcher ét ord sendes til Claude.
# Skriv korte rodformer så "energi" også matcher "energiforsyning", "energiret" osv.
DOMS_RELEVANTE_EMNEORD = {
    # Energi og forsyning
    "energi", "elforsyning", "gasforsyning", "fjernvarme", "varmeforsyning",
    "elnet", "naturgasforsyning", "vandforsyning", "havvind", "vindmølle",
    "solcelle", "biogas", "brint", "kraftvarme", "forsyningssikkerhed",
    # Miljø og natur
    "miljø", "forurening", "naturbeskyttelse", "miljøbeskyttelse",
    "spildevand", "drikkevand", "vandindvinding", "pesticid", "kemikalie",
    "affald", "luftforurening", "støj", "kyst", "natur",
    # Klima og afgifter
    "klima", "co2", "kuldioxid", "drivhusgas", "energiafgift",
    "kuldioxidafgift", "brændstofafgift",
    # Regulering og tilsyn
    "forsyningstilsynet", "energistyrelsen", "energiklagenævnet",
    "miljøstyrelsen", "naturstyrelsen",
    # Beredskab
    "beredskab", "kritisk infrastruktur",
}

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
    """
    Returnerer set af høring-URLs vi allerede har vist.
    Nyhedssider dedupes udelukkende via datofilteret (LOOKBACK_DAYS).
    Høringer har intet datofilter der forhindrer gentagelse, så de huskes.
    """
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return set(data.get("horinger_sete", []))
    except FileNotFoundError:
        print("  ℹ️  state.json ikke fundet — første kørsel")
        return set()
    except Exception as e:
        print(f"  ⚠️  Kunne ikke læse state.json: {e}")
        return set()


def save_state(horinger_sete):
    """
    Gemmer kun høring-URLs til state.json.
    Rydder automatisk op: fjerner høringer ældre end 60 dage
    (baseret på URL-mønsteret eller blot at vi beholder en rullende liste
    på maks 500 poster så filen ikke vokser ubegrænset).
    """
    # Behold maks 500 seneste poster (FIFO)
    sorteret = sorted(horinger_sete)
    if len(sorteret) > 500:
        sorteret = sorteret[-500:]

    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "horinger_sete": sorteret,
                    "last_updated":  datetime.now(timezone.utc).isoformat(),
                    "_note": "Kun høring-URLs gemmes. Nyhedssider dedupes via LOOKBACK_DAYS.",
                },
                f, indent=2, ensure_ascii=False,
            )
        print(f"  ✅ state.json gemt ({len(sorteret)} høringer husket)")
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
                {"emailAddress": {"address": r}} for r in RECIPIENTS
            ],
        },
        "saveToSentItems": True,
    }
    r = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
    r.raise_for_status()
    print("✅ Mail sendt.")


# ─────────────────────────────────────────────
# CLAUDE — KLASSIFICERING OG BESKRIVELSE
# ─────────────────────────────────────────────

# De 6 outputkategorier Lisa ønsker
KATEGORIER = [
    "Lovforslag",
    "Bekendtgørelser",
    "Vejledninger og praksis",
    "Politiske aftaler, strategier og udspil",
    "Høringer",
    "Domme og afgørelser",
    "Øvrige myndighedsnyheder",
]

# Farver pr. kategori (Outlook-kompatible)
KATEGORI_FARVER = {
    "Lovforslag":                              "#1A3A6B",
    "Bekendtgørelser":                         "#1A5276",
    "Vejledninger og praksis":                 "#1F618D",
    "Politiske aftaler, strategier og udspil": "#117A65",
    "Høringer":                                "#145A32",
    "Domme og afgørelser":                     "#7B241C",
    "Øvrige myndighedsnyheder":                "#4A235A",
}


def claude_klassificer(titel, kilde, url, uddrag=""):
    """
    Klassificerer nyheden i én af de 6 kategorier og genererer:
      - beskrivelse (2-4 linjer, neutral og deskriptiv)
      - bemærkninger (frist, ikrafttrædelse, status)
    uddrag: valgfrit tekstuddrag fra kilden (summary, resume, description)
            bruges til at generere mere præcise beskrivelser.
    Returnerer dict eller None ved fejl.
    """
    if not ANTHROPIC_API_KEY:
        return None

    uddrag_sektion = (
        f"\nUddrag fra kilden:\n{uddrag[:600]}\n"
        if uddrag and uddrag.strip() else ""
    )

    prompt = f"""Du er juridisk assistent for et dansk advokatfirma specialiseret i
energi- og forsyningsret, miljøret og regulatorisk ret.

Klassificér følgende nyhed fra en dansk myndighed og generér output til et ugentligt nyhedsoverblik.

Kilde: {kilde}
Titel: {titel}
URL: {url}{uddrag_sektion}

Advokatfirmaet specialiserer sig i: energiret, forsyningsret, elforsyning, gasforsyning,
fjernvarme, vedvarende energi, klimaregulering, miljøret, forsyningssikkerhed og beredskab.

Svar KUN med et JSON-objekt i præcis dette format (ingen præambel, ingen kodeblok):
{{
  "relevant": true,
  "kategori": "<én af: Lovforslag | Bekendtgørelser | Vejledninger og praksis | Politiske aftaler, strategier og udspil | Høringer | Domme og afgørelser | Øvrige myndighedsnyheder>",
  "beskrivelse": "<2-4 præcise sætninger der beskriver hvad nyheden konkret handler om — hvad ændres, hvad reguleres, hvad er formålet og konsekvensen. Brug uddraget hvis tilgængeligt. Neutral og deskriptiv, ingen vurdering. Tom streng hvis ikke relevant.>",
  "bemærkninger": "<høringsfrist, ikrafttrædelsesdato, status i lovgivningsprocessen — KUN hvis det fremgår eksplicit af titlen eller uddraget. Ellers: tom streng>"
}}

Regler:
- Sæt "relevant": false hvis indholdet IKKE vedrører energi, forsyning, klima, miljø eller beredskab
- Sæt "relevant": true hvis indholdet vedrører disse områder — selv indirekte (fx afgifter på energi, infrastruktur til elbiler, vandforsyning, naturbeskyttelse)
- Vælg Lovforslag hvis titlen indikerer et lovforslag fremsat for Folketinget
- Vælg Bekendtgørelser hvis titlen indikerer en bekendtgørelse eller ændring heraf
- Vælg Vejledninger og praksis hvis titlen indikerer en vejledning, retningslinje eller praksis
- Vælg Politiske aftaler, strategier og udspil hvis titlen indikerer en politisk aftale, strategi eller plan
- Vælg Høringer hvis titlen indikerer en høring eller udkast sendt i høring
- Vælg Domme og afgørelser hvis titlen indikerer en dom, kendelse, afgørelse eller retsafgørelse
- Vælg Øvrige myndighedsnyheder i alle andre tilfælde
- Sproget skal være sagligt, præcist og neutralt — egnet til professionel juridisk brug
- Beskriv hvad der faktisk fremgår af titlen og uddraget — antag, udled eller suppler ikke"""

    def _kald_api(prompt_text):
        return requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "Content-Type":      "application/json",
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
            },
            json={
                "model":      "claude-haiku-4-5",
                "max_tokens": 400,
                "messages":   [{"role": "user", "content": prompt_text}],
            },
            timeout=TIMEOUT,
        )

    try:
        r = _kald_api(prompt)
        if r.status_code == 429:
            print("  ⏳ Rate limit — venter 10 sek...")
            time.sleep(10)
            r = _kald_api(prompt)
        r.raise_for_status()
        raw = r.json()["content"][0]["text"].strip()
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
        data = json.loads(raw)
        # Valider kategori
        if data.get("kategori") not in KATEGORIER:
            data["kategori"] = "Øvrige myndighedsnyheder"
        return data
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
                # Hvis URL er set før men publiceret-dato er ny → opdateret høring
                er_opdateret = url in seen
                if not url or url in seen_urls:
                    continue
                # er_opdateret == True: markeres i results.append og vises med note

                # Hent content-feltet (rigt indhold + frist + type + status)
                content = entry.findtext("atom:content", "", ns) or ""
                content_clean = re.sub(r"<[^>]+>", " ", content)
                content_clean = re.sub(r"\s+", " ", content_clean).strip()

                # Udpak type, frist og status fra content-feltets afsluttende linje
                # Format: "Myndighed: X  Type: Y  Høringsfrist: DD-MM-YYYY  Status: Z"
                hoering_type = ""
                frist        = ""
                status       = ""

                type_match   = re.search(r"Type:\s*([^\n<]+?)(?:\s*Høringsfrist|\s*Status|$)", content_clean)
                frist_match  = re.search(r"Høringsfrist:\s*(\d{2}-\d{2}-\d{4})", content_clean)
                status_match = re.search(r"Status:\s*(\w+)", content_clean)

                if type_match:
                    hoering_type = type_match.group(1).strip()
                if frist_match:
                    frist = frist_match.group(1)
                if status_match:
                    status = status_match.group(1).strip()

                # Rens indholdet for metadata-linjen i bunden
                uddrag = re.sub(r"Myndighed:.*$", "", content_clean, flags=re.DOTALL).strip()

                seen_urls.add(url)
                results.append({
                    "kilde":     "Høringsportalen",
                    "myndighed": myndighed,
                    "titel":     titel,
                    "url":       url,
                    "dato":      dato,
                    "type":      hoering_type,
                    "frist":     frist,
                    "status":    status,
                    "summary":   uddrag[:600],
                    "opdateret": er_opdateret,
                })

        except ET.ParseError as e:
            print(f"  ⚠️  Høringsportalen ({myndighed}): XML-parse fejl: {e}")
        except Exception as e:
            print(f"  ⚠️  Høringsportalen ({myndighed}): {type(e).__name__}: {e}")

    print(f"  → Høringsportalen total: {len(results)} nye høringer")
    return results


# ─────────────────────────────────────────────
# FOLKETINGET — LOVFORSLAG VIA ODA API
# ─────────────────────────────────────────────

# FT_SAMLING er defineret som konstant øverst (fra env-var eller hardcoded)


def fetch_lovforslag(seen):
    """
    Henter fremsatte lovforslag fra Folketingets ODA API (oda.ft.dk).
    - Filtrerer på LOOKBACK_DAYS via opdateringsdato
    - Filtrerer på emneord så kun energi/forsyning/klima/miljø-relevante lovforslag medtages
    - Dedup mod seen
    Returnerer liste af dicts klar til build_html.
    """
    cutoff    = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    results   = []
    seen_urls = set()
    samling   = FT_SAMLING
    print(f"  → Bruger samling: {samling}")

    # Hent seneste 100 lovforslag — typeid=3 = lovforslag.
    # Dato-filtrering sker i Python nedenfor (OData-syntaks er upålidelig).
    url_api = (
        f"{FT_ODA_BASE}"
        f"?$filter=typeid%20eq%203"
        f"&$orderby=opdateringsdato%20desc"
        f"&$top=100"
        f"&$format=json"
    )

    try:
        r = requests.get(url_api, headers=SCRAPE_HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"  ⚠️  Folketinget ODA: HTTP {r.status_code} — API muligvis blokeret eller utilgængeligt")
            print(f"     URL forsøgt: {url_api[:100]}")
            return results

        data  = r.json()
        items = data.get("value", [])
        print(f"  → Folketinget ODA: {len(items)} lovforslag hentet (seneste {LOOKBACK_DAYS} dage)")

        frafiltreret = 0
        for item in items:
            titel = (item.get("titel") or item.get("titelkort") or "").strip()
            if not titel:
                continue

            titel_lower = titel.lower()
            if any(re.search(r"\b" + re.escape(o) + r"\w*", titel_lower)
                   for o in FT_UDELUK_EMNEORD):
                frafiltreret += 1
                continue

            # Byg URL til ft.dk
            nummer  = item.get("nummer", "").replace(" ", "")  # "L 5" → "L5"
            sml     = item.get("samlingid") or samling
            if nummer:
                ft_url = f"https://www.ft.dk/samling/{sml}/lovforslag/{nummer}/index.htm"
            else:
                ft_url = "https://www.ft.dk/da/dokumenter/dokumentlister/lovforslag"

            if ft_url in seen or ft_url in seen_urls:
                continue

            # Dato
            dato_str = item.get("opdateringsdato") or item.get("fremsatdato") or ""
            dato     = None
            if dato_str:
                try:
                    dato = datetime.fromisoformat(
                        dato_str.replace("Z", "+00:00")
                    ).replace(tzinfo=timezone.utc)
                except ValueError:
                    dato = _parse_danish_date(dato_str)

            # Dato-filter — spring over hvis ældre end LOOKBACK_DAYS
            if dato and dato < cutoff:
                continue

            # Status fra ODA API — fx "Fremsat", "Til 2. behandling", "Vedtaget"
            status = (item.get("status") or "").strip()

            # Resume fra ODA — kort beskrivelse af lovforslagets indhold
            resume = re.sub(r'\s+', ' ', (item.get('resume') or '').replace('\\n', ' ')).strip()

            seen_urls.add(ft_url)
            results.append({
                "kilde_id":     "ft",
                "navn":         "Folketinget",
                "titel":        titel,
                "url":          ft_url,
                "dato":         dato,
                "farve":        "#1A3A6B",
                "kategori":     "Lovforslag",
                "beskrivelse":  "",
                "bemærkninger": f"Status: {status}" if status else "",
                "uddrag":       resume[:600],
            })

        if frafiltreret:
            print(f"  → {frafiltreret} lovforslag forfilteret (åbenlyst irrelevante) — Claude vurderer resten")

    except Exception as e:
        print(f"  ⚠️  Folketinget ODA: {type(e).__name__}: {e}")

    print(f"  → Folketinget ODA: {len(results)} relevante lovforslag")
    return results


# ─────────────────────────────────────────────
# DOMSDATABASEN — DOMME OG KENDELSER
# ─────────────────────────────────────────────

def fetch_domsdatabasen(seen):
    """
    Henter danske domme og kendelser fra Domsdatabasen via RSS.
    Returnerer liste af dicts klar til Claude-klassificering.
    Domsdatabasen returnerer seneste 7 dages domme — matcher LOOKBACK_DAYS.
    """
    cutoff    = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    results   = []
    seen_urls = set()

    try:
        r = requests.get(DOMS_RSS_URL, headers=SCRAPE_HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"  ⚠️  Domsdatabasen: HTTP {r.status_code}")
            return results

        root    = ET.fromstring(r.content)
        channel = root.find("channel") or root
        items   = channel.findall("item") or root.findall(".//item")
        print(f"  → Domsdatabasen: {len(items)} domme i feed")

        for entry in items:
            def _get(tag):
                el = entry.find(tag)
                return el.text.strip() if el is not None and el.text else ""

            titel    = _get("title")
            url      = _get("link")
            date_str = _get("pubDate")
            desc     = _get("description")

            if not titel or not url:
                continue
            if url in seen or url in seen_urls:
                continue

            # Dato
            dato = None
            for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S %Z",
                        "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
                try:
                    dato = datetime.strptime(date_str[:31].strip(), fmt)
                    if not dato.tzinfo:
                        dato = dato.replace(tzinfo=timezone.utc)
                    break
                except ValueError:
                    continue

            if dato and dato < cutoff:
                continue

            # Forfilter — kun domme med relevante emneord i titel eller beskrivelse
            søgetekst = f"{titel} {desc}".lower()
            if not any(re.search(r"\b" + re.escape(o) + r"\w*", søgetekst)
                       for o in DOMS_RELEVANTE_EMNEORD):
                continue

            seen_urls.add(url)
            results.append({
                "kilde_id":     "doms",
                "navn":         "Domsdatabasen",
                "titel":        titel,
                "url":          url,
                "dato":         dato,
                "farve":        "#7B241C",
                "beskrivelse":  desc[:200] if desc else "",
                "bemærkninger": "",
                "kategori":     "",  # Sættes af Claude
            })

    except Exception as e:
        print(f"  ⚠️  Domsdatabasen: {type(e).__name__}: {e}")

    print(f"  → Domsdatabasen: {len(results)} domme efter emneforfilter (Claude vurderer resten)")
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


def _normalise_url(href, base_url):
    """Gør en relativ href til fuld URL."""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        p = urlparse(base_url)
        return f"{p.scheme}://{p.netloc}{href}"
    return base_url.rstrip("/") + "/" + href


# URL-segmenter og titler der indikerer navigation/footer — ikke nyheder
_NAV_URL_SEGMENTER = {
    "kontakt", "contact", "om-os", "about", "job", "ledige-stillinger",
    "cookies", "privatlivs", "persondatapolitik", "tilgaengelighedserklaering",
    "was.digst", "whistleblower", "energimaerkning", "energieffektivitetsplan",
    "abonner", "nyhedsbrev", "tilmeld", "linkedin", "twitter", "bluesky",
    "facebook", "instagram", "youtube", "rss", "sitemap", "login",
    "english", "borger", "erhverv", "publikationer", "parkering",
    "aktindsigt", "annoncer", "presse-kontakt", "digital-post",
}

_NAV_TITLER = {
    "kontakt", "om os", "job", "cookies", "english", "borger", "erhverv",
    "publikationer", "parkering", "aktindsigt", "annoncering", "annonceringer",
    "tilgængelighedserklæring", "whistleblowerordning", "persondatapolitik",
    "nyhedsbrev", "tilmeld nyhedsbrev", "følg os", "nyttige links", "genveje",
    "ledige stillinger", "linkedin", "x", "bluesky", "facebook",
    "energimærkning", "energieffektivitetsplan", "seneste nyheder",
    "privatlivs- og cookiepolitik", "miljøministeriet", "kystdirektoratet",
    "retningslinjer for brug af sociale medier",
    "retningslinjer for aktiv informationspligt",
    "om miljøstyrelsen", "om energistyrelsen", "om forsyningstilsynet",
    "om naturstyrelsen", "om ministeriet", "om kefm",
}


def _er_nav_link(titel, url):
    """Returnerer True hvis linket ser ud til at være navigation/footer."""
    titel_lower = titel.strip().lower()
    if titel_lower in _NAV_TITLER:
        return True
    # "Om X" er næsten altid en om-siden, ikke en nyhed
    if titel_lower.startswith("om ") and len(titel_lower) < 40:
        return True
    # Tjek URL-segmenter
    path = urlparse(url).path.lower()
    for seg in _NAV_URL_SEGMENTER:
        if f"/{seg}" in path or path.endswith(seg):
            return True
    # For kort titel er det sandsynligvis navigation
    if len(titel.strip()) < 8:
        return True
    return False


def _html_items_to_news(items, source, seen, cutoff):
    """Konverterer (titel, href, dato_text)-tupler til news-dicts.
    Filtrerer navigation og footer-links fra.
    """
    results = []
    seen_urls = set()
    for titel, href, dato_text in items:
        full_url = _normalise_url(href, source["url"])
        # Filtrer nav/footer
        if _er_nav_link(titel, full_url):
            continue
        if full_url in seen or full_url in seen_urls:
            continue
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
    return results


def _fetch_rss_source(source, seen):
    """
    Henter nyheder fra et RSS/ATOM-feed.
    Bruges til KEFM der har officielt RSS-feed.
    """
    cutoff  = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    results = []
    seen_urls = set()

    try:
        r = requests.get(source["url"], headers=SCRAPE_HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"  ⚠️  {source['navn']} RSS: HTTP {r.status_code}")
            return results

        root = ET.fromstring(r.content)
        # Prøv både RSS <item> og ATOM <entry>
        ns_atom = {"atom": "http://www.w3.org/2005/Atom"}

        # RSS 2.0
        items_rss = root.findall(".//item")
        # ATOM
        items_atom = root.findall("atom:entry", ns_atom)
        all_entries = items_rss or items_atom

        base = urlparse(source["url"])
        base_url = f"{base.scheme}://{base.netloc}"

        for entry in all_entries:
            # Titel
            titel = (entry.findtext("title") or
                     entry.findtext("atom:title", namespaces=ns_atom) or "").strip()
            if not titel:
                continue

            # URL
            link_el = entry.find("link")
            if link_el is not None and link_el.text:
                url = link_el.text.strip()
            elif link_el is not None and link_el.get("href"):
                url = link_el.get("href")
            else:
                url_el = entry.find("atom:link", ns_atom)
                url = url_el.get("href", "") if url_el is not None else ""

            if not url:
                continue
            if not url.startswith("http"):
                url = base_url + url

            # Dato
            pub = (entry.findtext("pubDate") or
                   entry.findtext("published") or
                   entry.findtext("atom:published", namespaces=ns_atom) or
                   entry.findtext("updated") or
                   entry.findtext("atom:updated", namespaces=ns_atom) or "")
            dato = None
            if pub:
                try:
                    # RSS pubDate: "Mon, 23 Jun 2026 00:00:00 +0000"
                    dato = parsedate_to_datetime(pub).replace(tzinfo=timezone.utc)
                except Exception:
                    dato = _parse_danish_date(pub)

            if dato and dato < cutoff:
                continue
            if url in seen or url in seen_urls:
                continue

            # Hent description/summary fra feed til brug i Claude-beskrivelse
            desc = (entry.findtext("description") or
                    entry.findtext("summary") or
                    entry.findtext("atom:summary", namespaces=ns_atom) or "").strip()
            desc = re.sub(r'<[^>]+>', ' ', desc)
            desc = re.sub(r'\s+', ' ', desc).strip()

            seen_urls.add(url)
            results.append({
                "kilde_id": source["id"],
                "navn":     source["navn"],
                "titel":    titel,
                "url":      url,
                "dato":     dato,
                "farve":    source["farve"],
                "uddrag":   desc[:600],
            })

        print(f"  → {source['navn']} RSS: {len(results)} nye nyheder")
    except Exception as e:
        print(f"  ⚠️  {source['navn']} RSS: {type(e).__name__}: {e}")

    return results


def _fetch_html_monthly(source, seen):
    """
    Henter nyheder fra sites der organiserer artikler på månedssider
    (fx forsyningstilsynet.dk/nyheder/YYYY/mmm).
    Checker indeværende måned og forrige måned hvis LOOKBACK_DAYS > 28.
    """
    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=LOOKBACK_DAYS)

    MAANED_KORT = {
        1: "jan", 2: "feb", 3: "mar", 4: "apr",
        5: "maj", 6: "jun", 7: "jul", 8: "aug",
        9: "sep", 10: "okt", 11: "nov", 12: "dec",
    }

    # Byg liste af (år, måned) der skal hentes
    months_to_check = [(now.year, now.month)]
    # Tilføj forrige måned hvis cutoff er i den
    prev = now.replace(day=1) - timedelta(days=1)
    if prev >= cutoff:
        months_to_check.append((prev.year, prev.month))

    results = []
    seen_urls = set()

    for year, month in months_to_check:
        url = source["url"].format(year=year, month=MAANED_KORT[month])
        try:
            r = requests.get(url, headers=SCRAPE_HEADERS, timeout=TIMEOUT)
            if r.status_code == 404:
                print(f"  ℹ️  {source['navn']}: ingen artikler for {MAANED_KORT[month]} {year}")
                continue
            if r.status_code != 200:
                print(f"  ⚠️  {source['navn']}: HTTP {r.status_code} for {url}")
                continue

            soup  = BeautifulSoup(r.text, "html.parser")
            items = _find_articles(soup, url)
            print(f"  → {source['navn']} ({MAANED_KORT[month]} {year}): {len(items)} artikler")

            for titel, href, dato_text in items:
                full_url = _normalise_url(href, url)
                if full_url in seen or full_url in seen_urls:
                    continue
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
            print(f"  ⚠️  {source['navn']} ({MAANED_KORT[month]}): {type(e).__name__}: {e}")

    print(f"  → {source['navn']}: {len(results)} nye nyheder efter dedup")
    return results


def fetch_news_source(source, seen):
    """
    Router til den rigtige hente-metode baseret på source["type"]:
      "rss"         → RSS/ATOM-feed
      "html_monthly"→ HTML-scraping af månedssider
      "html"        → HTML-scraping af listeside (generisk)
    """
    source_type = source.get("type", "html")

    if source_type == "rss":
        return _fetch_rss_source(source, seen)

    if source_type == "html_monthly":
        return _fetch_html_monthly(source, seen)

    # Standard HTML-scraping
    cutoff  = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    results = []
    try:
        r = requests.get(source["url"], headers=SCRAPE_HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            print(f"  ⚠️  {source['navn']}: HTTP {r.status_code}")
            return results

        soup  = BeautifulSoup(r.text, "html.parser")
        items = _find_articles(soup, source["url"])
        print(f"  → {source['navn']}: {len(items)} artikler fundet på siden")
        results = _html_items_to_news(items, source, seen, cutoff)
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

HULGAARD_LOGO_URL = "data:image/png;base64,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"


def _format_dato(dato):
    """Returnerer dansk datostreng, fx '12. juni 2026'."""
    if not dato:
        return ""
    MAANEDER = [
        "", "januar", "februar", "marts", "april", "maj", "juni",
        "juli", "august", "september", "oktober", "november", "december",
    ]
    return f"{dato.day}. {MAANEDER[dato.month]} {dato.year}"


def _item_html(item, farve):
    """Returnerer HTML-blok for ét element (nyhed eller høring) med Lisas format."""
    dato_str    = _format_dato(item.get("dato"))
    kilde_navn  = item.get("navn", item.get("myndighed", ""))
    beskrivelse = item.get("beskrivelse", "")
    bemærkninger = item.get("bemærkninger", "")
    frist       = item.get("frist", "")

    # Meta-linje: kilde · dato
    meta_dele = []
    if kilde_navn:
        meta_dele.append(kilde_navn)
    if dato_str:
        meta_dele.append(dato_str)
    meta_html = (
        f'<p style="margin:0 0 4px 0;font-size:11px;color:#888;">' +
        " · ".join(meta_dele) + "</p>"
        if meta_dele else ""
    )

    besk_html = (
        f'<p style="margin:6px 0 0 0;font-size:13px;color:#333;">{beskrivelse}</p>'
        if beskrivelse else ""
    )

    # Bemærkninger: brug frist fra høring hvis tilgængeligt
    bem_tekst = bemærkninger or (f"Høringsfrist: {frist}" if frist and frist != "ikke angivet" else "")
    # Fjern "Bemærkninger:"-præfiks hvis Claude allerede har sat det
    if bem_tekst.lower().startswith("bemærkninger:"):
        bem_tekst = bem_tekst[len("bemærkninger:"):].strip()
    # Skjul feltet helt hvis der ikke er noget reelt at vise
    if bem_tekst.lower() in ("ikke angivet", "ingen bemærkninger", ""):
        bem_tekst = ""
    bem_html = (
        f'<p style="margin:5px 0 0 0;font-size:12px;color:#555;">'
        f'<strong>Bemærkninger:</strong> {bem_tekst}</p>'
        if bem_tekst else ""
    )

    return f"""
<table width="100%" cellpadding="0" cellspacing="0" border="0"
       style="margin-bottom:10px;border-left:3px solid {farve};
              background:#F4F6F8;">
  <tr>
    <td style="padding:10px 14px;">
      {meta_html}
      <p style="margin:0;font-size:14px;font-weight:bold;">
        <a href="{item['url']}" style="color:{BRAND_DARK};text-decoration:none;">
          {item['titel']}
        </a>
      </p>
      {besk_html}
      {bem_html}
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
    Bygger den fulde HTML-e-mail grupperet i Lisas 6 kategorier.
    news_by_source: dict  kilde_id → liste af nyheds-dicts (med "kategori"-felt)
    hearings:       liste af høring-dicts (placeres i "Høringer")
    """
    now      = datetime.now(timezone.utc)
    dato_str = _format_dato(now)

    # Saml alle elementer i én liste og tilføj høringer som "Høringer"-kategori
    alle_items = []
    for items in news_by_source.values():
        alle_items.extend(items)
    for h in hearings:
        h_copy = dict(h)
        h_copy["kategori"] = "Høringer"
        alle_items.append(h_copy)

    total = len(alle_items)

    subject = (
        f"Energi & Forsyning — ugentlig opdatering {dato_str} "
        f"({total} {'element' if total == 1 else 'elementer'})"
    )

    # Gruppér i de 6 kategorier i Lisas rækkefølge
    fra_kategori = {k: [] for k in KATEGORIER}
    for item in alle_items:
        kat = item.get("kategori", "Øvrige myndighedsnyheder")
        if kat not in fra_kategori:
            kat = "Øvrige myndighedsnyheder"
        fra_kategori[kat].append(item)

    # Byg sektioner — kun kategorier med indhold
    sektioner_html = ""
    for kat in KATEGORIER:
        items = fra_kategori[kat]
        if not items:
            continue
        farve  = KATEGORI_FARVER[kat]
        blokke = "".join(_item_html(it, farve) for it in items)
        sektioner_html += _sektion_html(f"{kat} ({len(items)})", farve, blokke)

    total_nyheder  = sum(len(v) for v in news_by_source.values())
    total_horinger = len(hearings)

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
              fra myndighederne og
              <strong>{total_horinger} {'høring' if total_horinger == 1 else 'høringer'}</strong>
              på Høringsportalen · i alt {total} elementer.
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
              Kilder: KEFM, Energinet, Miljøministeriet, Green Power Denmark, Dansk Fjernvarme, EU-Kommissionen, Høringsportalen, Folketinget, Domsdatabasen
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
    if not RECIPIENTS:
        raise RuntimeError("Ingen modtagere sat — tjek RECIPIENT_1 til RECIPIENT_7")
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

    # ── Hent lovforslag fra Folketinget ──
    print("▶ Henter lovforslag fra Folketinget ODA API...")
    lovforslag = fetch_lovforslag(seen)

    # ── Hent domme fra Domsdatabasen ──
    print("▶ Henter domme fra Domsdatabasen...")
    domme = fetch_domsdatabasen(seen)

    # ── Hent nyheder fra nyhedskilder (KEFM RSS) ──
    news_by_source = {}
    for source in NEWS_SOURCES:
        print(f"▶ Henter nyheder fra {source['navn']}...")
        items = fetch_news_source(source, set())
        if items:
            news_by_source[source["id"]] = items

    # Tilføj lovforslag og domme som egne kilder
    if lovforslag:
        news_by_source["ft"] = lovforslag
    if domme:
        news_by_source["doms"] = domme

    # ── AI-klassificering: nyheder (kategori + beskrivelse + bemærkninger) ──
    if ANTHROPIC_API_KEY:
        alle_nyheder = [it for items in news_by_source.values() for it in items]
        print(f"▶ Klassificerer {len(alle_nyheder)} nyheder med Claude...")
        kasserede = 0
        for it in alle_nyheder:
            analyse = claude_klassificer(it["titel"], it["navn"], it["url"], it.get("uddrag", ""))
            if analyse:
                if not analyse.get("relevant", True):
                    kasserede += 1
                    for kilde_items in news_by_source.values():
                        if it in kilde_items:
                            kilde_items.remove(it)
                    time.sleep(8)
                    continue
                it["kategori"]    = analyse.get("kategori", "Øvrige myndighedsnyheder")
                it["beskrivelse"] = analyse.get("beskrivelse", "")
                claude_bem        = analyse.get("bemærkninger", "")
                if not (it.get("bemærkninger") and it["bemærkninger"] not in ("", "ikke angivet")):
                    it["bemærkninger"] = claude_bem if (claude_bem and claude_bem.lower() not in ("ikke angivet", "")) else ""
                print(f"  → [{it['kategori']}] {it['titel'][:50]}")
            else:
                it.setdefault("kategori", "Øvrige myndighedsnyheder")
            time.sleep(8)
        if kasserede:
            print(f"  → {kasserede} kasseret af Claude (ikke relevant for energi/forsyning/klima/miljø)")

        # ── AI-beskrivelse for høringer — alle der vises i mailen ──
        # hearings inkluderer kun nye (ikke-sete), men vi beskriver dem alle
        print(f"▶ Genererer beskrivelser for {len(hearings)} høringer med Claude...")
        for it in hearings:
            if it.get("beskrivelse"):
                continue  # Allerede beskrevet
            analyse = claude_klassificer(it["titel"], it["myndighed"], it["url"], it.get("summary", ""))
            if analyse:
                beskrivelse = analyse.get("beskrivelse", "")
                # Tilføj note hvis høringen er en opdatering af en tidligere set høring
                if it.get("opdateret") and beskrivelse:
                    beskrivelse = f"Opdateret høringsnotat. {beskrivelse}"
                elif it.get("opdateret"):
                    beskrivelse = "Opdateret høringsnotat publiceret på Høringsportalen."
                it["beskrivelse"] = beskrivelse

            # Sæt bemærkninger med høringsfrist og status
            frist  = it.get("frist", "")
            status = it.get("status", "")
            bem_dele = []
            if frist:
                bem_dele.append(f"Høringsfrist: {frist}")
            if status and status.lower() not in ("igang",):
                # Vis status hvis den er andet end normal "Igang"
                bem_dele.append(f"Status: {status}")
            it["bemærkninger"] = " · ".join(bem_dele) if bem_dele else ""
            time.sleep(8)
    else:
        # Uden AI: sæt alle nyheder i øvrige
        for items in news_by_source.values():
            for it in items:
                it.setdefault("kategori", "Øvrige myndighedsnyheder")

    # ── Opdatér state — høringer + lovforslag ──
    # Kun NYE høringer (ikke opdaterede) tilføjes til seen
    nye_hoering_urls    = {it["url"] for it in hearings if not it.get("opdateret")}
    nye_lovforslag_urls = {it["url"] for it in news_by_source.get("ft", [])}
    save_state(seen | nye_hoering_urls | nye_lovforslag_urls)

    # ── Byg HTML ──
    print("▶ Bygger HTML-mail...")
    subject, html = build_html(news_by_source, hearings)

    # ── Hent token og send mail ──
    print("▶ Henter Microsoft Graph-token...")
    token = get_token()

    print(f"▶ Sender mail til {len(RECIPIENTS)} modtagere: {', '.join(RECIPIENTS)}...")
    send_mail(token, subject, html)
    print("✅ Færdig.")
