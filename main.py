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
# Alle modtagere samlet — tilføj RECIPIENT_3 osv. efter behov
RECIPIENTS = [r for r in [RECIPIENT_1, RECIPIENT_2] if r]
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

                # Udpak ekstra metadata fra summary
                summary = entry.findtext("atom:summary", "", ns) or ""
                summary_clean = re.sub(r"<[^>]+>", " ", summary)
                summary_clean = re.sub(r"\s+", " ", summary_clean).strip()

                # Forsøg at udpak type og frist fra summary
                hoering_type = ""
                frist        = ""

                # Summary-format: "Høringstype X · Myndighed Y · Høringsfrist DD-MM-YYYY · ..."
                # Både · og almindelige mellemrum bruges som separator
                type_match  = re.search(r"Høringstype[:\s]+([^·\n]+?)(?:\s*·|\s+Myndighed|\s+Høringsfrist|$)", summary_clean)
                frist_match = re.search(r"Høringsfrist[:\s]+(\d{2}-\d{2}-\d{4})", summary_clean)
                if type_match:
                    hoering_type = type_match.group(1).strip().rstrip("·").strip()
                if frist_match:
                    frist = frist_match.group(1)
                    print(f"     Frist fundet: {frist} ({titel[:40]})")

                seen_urls.add(url)
                results.append({
                    "kilde":     "Høringsportalen",
                    "myndighed": myndighed,
                    "titel":     titel,
                    "url":       url,
                    "dato":      dato,
                    "type":      hoering_type,
                    "frist":     frist,
                    "summary":   summary_clean[:300],
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
                  <table cellpadding="0" cellspacing="0" border="0">
                    <tr>
                      <td style="background:#ffffff;padding:6px 10px;border-radius:4px;">
                        <img src="{HULGAARD_LOGO_URL}" alt="Hulgaard Advokater"
                             height="32"
                             style="display:block;border:0;height:32px;" />
                      </td>
                    </tr>
                  </table>
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

    print(f"▶ Sender mail til {RECIPIENT_1} og {RECIPIENT_2}...")
    send_mail(token, subject, html)
    print("✅ Færdig.")
