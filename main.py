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
import os as _os
FT_SAMLING = _os.environ.get("FT_SAMLING", "20252").strip() or "20252"

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


def claude_klassificer(titel, kilde, url):
    """
    Klassificerer nyheden i én af de 6 kategorier og genererer:
      - beskrivelse (2-4 linjer, neutral og deskriptiv)
      - bemærkninger (frist, ikrafttrædelse, status — eller "ikke angivet")
    Returnerer dict eller None ved fejl.
    """
    if not ANTHROPIC_API_KEY:
        return None

    prompt = f"""Du er juridisk assistent for et dansk advokatfirma specialiseret i
energi- og forsyningsret, miljøret og regulatorisk ret.

Klassificér følgende nyhed fra en dansk myndighed og generér output til et ugentligt nyhedsoverblik.

Kilde: {kilde}
Titel: {titel}
URL: {url}

Advokatfirmaet specialiserer sig i: energiret, forsyningsret, elforsyning, gasforsyning,
fjernvarme, vedvarende energi, klimaregulering, miljøret, forsyningssikkerhed og beredskab.

Svar KUN med et JSON-objekt i præcis dette format (ingen præambel, ingen kodeblok):
{{
  "relevant": true,
  "kategori": "<én af: Lovforslag | Bekendtgørelser | Vejledninger og praksis | Politiske aftaler, strategier og udspil | Høringer | Domme og afgørelser | Øvrige myndighedsnyheder>",
  "beskrivelse": "<2-4 linjer, neutral og deskriptiv beskrivelse af hovedindhold og formål. Ingen vurdering eller fortolkning. Tom streng hvis ikke relevant.>",
  "bemærkninger": "<høringsfrist, ikrafttrædelsesdato, status i lovgivningsprocessen — kun hvis det fremgår eksplicit af titlen. Ellers: ikke angivet>"
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
- Beskriv kun hvad titlen angiver — antag, udled eller suppler ikke"""

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
                if not url or url in seen or url in seen_urls:
                    continue

                # Udpak ekstra metadata fra summary
                summary = entry.findtext("atom:summary", "", ns) or ""
                summary_clean = re.sub(r"<[^>]+>", " ", summary)
                summary_clean = re.sub(r"\s+", " ", summary_clean).strip()

                # Forsøg at udpak type og frist fra summary
                hoering_type = ""
                frist        = "ikke angivet"

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
    from urllib.parse import quote
    cutoff    = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    cutoff    = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)
    results   = []
    seen_urls = set()
    samling   = FT_SAMLING
    print(f"  → Bruger samling: {samling}")

    # Hent seneste 100 lovforslag — dato-filtrering sker i Python nedenfor
    # (ODA API v3 dato-filter-syntaks er upålidelig)
    # typeid=3 = lovforslag, sorteret efter opdateringsdato
    # Dato-filtrering sker i Python nedenfor (OData dato-filter er upålidelig)
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

            import re as _re
            titel_lower = titel.lower()
            if any(_re.search(r"\b" + _re.escape(o) + r"\w*", titel_lower)
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

            # Status til bemærkninger
            status = item.get("status", "")

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
                "bemærkninger": status if status else "ikke angivet",
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
    import xml.etree.ElementTree as ET
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
            import re as _re
            søgetekst = f"{titel} {desc}".lower()
            if not any(_re.search(r"\b" + _re.escape(o) + r"\w*", søgetekst)
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
                "bemærkninger": "ikke angivet",
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
    from urllib.parse import urlparse
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
    from urllib.parse import urlparse
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
    from urllib.parse import urlparse
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
                    from email.utils import parsedate_to_datetime
                    dato = parsedate_to_datetime(pub).replace(tzinfo=timezone.utc)
                except Exception:
                    dato = _parse_danish_date(pub)

            if dato and dato < cutoff:
                continue
            if url in seen or url in seen_urls:
                continue

            seen_urls.add(url)
            results.append({
                "kilde_id": source["id"],
                "navn":     source["navn"],
                "titel":    titel,
                "url":      url,
                "dato":     dato,
                "farve":    source["farve"],
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
    if not bem_tekst:
        bem_tekst = "ikke angivet"
    # Fjern "Bemærkninger:"-præfiks hvis Claude allerede har sat det
    if bem_tekst.lower().startswith("bemærkninger:"):
        bem_tekst = bem_tekst[len("bemærkninger:"):].strip()
    bem_html = (
        f'<p style="margin:5px 0 0 0;font-size:12px;color:#555;">' +
        f'<strong>Bemærkninger:</strong> {bem_tekst}</p>'
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


def _nyhed_html(item):
    """Wrapper — bruges stadig fra ældre kode."""
    farve = KATEGORI_FARVER.get(item.get("kategori", ""), item.get("farve", "#1A5276"))
    return _item_html(item, farve)


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
        for it in list(alle_nyheder):
            analyse = claude_klassificer(it["titel"], it["navn"], it["url"])
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
                if not (it.get("bemærkninger") and it["bemærkninger"] != "ikke angivet"):
                    it["bemærkninger"] = claude_bem if (claude_bem and claude_bem.lower() != "ikke angivet") else "ikke angivet"
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
            analyse = claude_klassificer(it["titel"], it["myndighed"], it["url"])
            if analyse:
                it["beskrivelse"] = analyse.get("beskrivelse", "")
            time.sleep(8)
    else:
        # Uden AI: sæt alle nyheder i øvrige
        for items in news_by_source.values():
            for it in items:
                it.setdefault("kategori", "Øvrige myndighedsnyheder")

    # ── Opdatér state — høringer + lovforslag ──
    nye_hoering_urls  = {it["url"] for it in hearings}
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
