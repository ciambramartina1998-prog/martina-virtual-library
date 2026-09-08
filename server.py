import os
import json
import re
import threading
import time
from html import unescape

from urllib.parse import urlparse, parse_qs, quote_plus
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import psycopg


PORT = int(os.environ.get("PORT", "8000"))

GOOGLE_BOOKS_API_KEY = os.environ.get(
    "GOOGLE_BOOKS_API_KEY",
    ""
)

DATABASE_URL = os.environ.get(
    "DATABASE_URL"
)

# Se Google Books risponde 429, lo mettiamo temporaneamente in pausa.
# Render condivide spesso gli IP tra più servizi: continuare a richiamare Google
# durante il blocco peggiora soltanto il rate limit.
GOOGLE_BOOKS_PAUSA_SECONDI = int(
    os.environ.get("GOOGLE_BOOKS_PAUSA_SECONDI", "21600")  # 6 ore
)
_google_books_bloccato_fino = 0.0
_google_books_lock = threading.Lock()


def google_books_disponibile():
    with _google_books_lock:
        return time.time() >= _google_books_bloccato_fino


def blocca_temporaneamente_google_books():
    global _google_books_bloccato_fino
    with _google_books_lock:
        _google_books_bloccato_fino = max(
            _google_books_bloccato_fino,
            time.time() + GOOGLE_BOOKS_PAUSA_SECONDI
        )


def parametro_chiave_google():
    """Aggiunge la chiave Google Books quando configurata su Render."""
    if GOOGLE_BOOKS_API_KEY:
        return "&key=" + quote_plus(GOOGLE_BOOKS_API_KEY)
    return ""


def connessione_database():

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL non configurato."
        )

    return psycopg.connect(
        DATABASE_URL
    )


def libro_json(riga):

    return {
        "id": riga[0],
        "titolo": riga[1],
        "categoria": riga[2],
        "stato": riga[3],
        "copertina": riga[4],
        "lettura_attuale": riga[5],
        "preferito": riga[6],
        "voto": riga[7],
        "trama": riga[8] or "",
        "creato_il": (
            riga[9].isoformat()
            if riga[9]
            else None
        )
    }


def scarica_json(url):

    richiesta = Request(
        url,
        headers={
            "User-Agent":
                "MartinaVirtualLibrary/1.0",
            "Accept":
                "application/json"
        }
    )

    with urlopen(
        richiesta,
        timeout=20
    ) as risposta:

        return json.loads(
            risposta.read().decode(
                "utf-8"
            )
        )




def scarica_testo(url):
    richiesta = Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; MartinaVirtualLibrary/1.0)",
            "Accept": "text/html,application/xhtml+xml"
        }
    )
    with urlopen(richiesta, timeout=20) as risposta:
        return risposta.read().decode("utf-8", errors="ignore")


def _meta_html(html, nome):
    patterns = [
        rf'<meta[^>]+property=["\']{re.escape(nome)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']{re.escape(nome)}["\']',
        rf'<meta[^>]+name=["\']{re.escape(nome)}["\'][^>]+content=["\']([^"\']+)["\']',
        rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']{re.escape(nome)}["\']',
    ]
    for pattern in patterns:
        m = re.search(pattern, html, flags=re.IGNORECASE)
        if m:
            return unescape(m.group(1)).strip()
    return ""


def cerca_google_books_html(titolo):
    """Fallback senza API: usa le pagine pubbliche di Google Books."""
    titolo = str(titolo or "").strip()
    if not titolo:
        return {}

    try:
        ricerca_url = (
            "https://books.google.com/books?q="
            + quote_plus('intitle:"' + titolo + '"')
            + "&hl=it"
        )
        html = scarica_testo(ricerca_url)
    except Exception as errore:
        print("Google Books HTML ricerca non disponibile:", errore, flush=True)
        return {}

    ids = []
    for pattern in [
        r'/books\?id=([A-Za-z0-9_-]+)',
        r'[?&]id=([A-Za-z0-9_-]{6,})',
    ]:
        for book_id in re.findall(pattern, html):
            if book_id not in ids:
                ids.append(book_id)

    candidati = []
    for book_id in ids[:8]:
        try:
            pagina = scarica_testo(
                "https://books.google.com/books?id=" + quote_plus(book_id) + "&hl=it"
            )
        except Exception:
            continue

        titolo_trovato = _meta_html(pagina, "og:title")
        if not titolo_trovato:
            m = re.search(r'<title>(.*?)</title>', pagina, flags=re.I | re.S)
            titolo_trovato = unescape(re.sub(r'<[^>]+>', '', m.group(1))).strip() if m else ""

        punteggio = punteggio_titolo_trama(titolo, titolo_trovato)
        if punteggio <= 0:
            continue

        copertina = _meta_html(pagina, "og:image")
        trama = _meta_html(pagina, "description") or _meta_html(pagina, "og:description")
        trama = pulisci_trama_google(trama)
        if sembra_trama_inglese(trama):
            trama = ""

        if copertina:
            punteggio += 15
        if trama:
            punteggio += 20

        candidati.append((punteggio, {
            "titolo": titolo_trovato,
            "copertina": copertina,
            "trama": trama,
            "autori": [],
            "lingua": "it",
            "fonte": "Google Books HTML"
        }))

    if not candidati:
        return {}

    candidati.sort(key=lambda x: x[0], reverse=True)
    return dict(candidati[0][1])


def scarica_json_google(url):
    """Scarica JSON da Google Books rispettando il cooldown dopo un 429."""
    if not google_books_disponibile():
        return None

    try:
        return scarica_json(url)
    except HTTPError as errore:
        if errore.code == 429:
            blocca_temporaneamente_google_books()
            print(
                "⏸️ Google Books in pausa dopo HTTP 429; uso Open Library.",
                flush=True
            )
            return None
        raise


# ==========================================
# TRAMA AUTOMATICA DA GOOGLE BOOKS
# ==========================================

def pulisci_trama_google(trama):

    if not trama:
        return ""

    testo = str(trama)

    testo = re.sub(
        r"<br\s*/?>",
        "\n",
        testo,
        flags=re.IGNORECASE
    )

    testo = re.sub(
        r"</p\s*>",
        "\n\n",
        testo,
        flags=re.IGNORECASE
    )

    testo = re.sub(
        r"<[^>]+>",
        "",
        testo
    )

    testo = unescape(
        testo
    )

    testo = re.sub(
        r"[ \t]+",
        " ",
        testo
    )

    testo = re.sub(
        r"\n{3,}",
        "\n\n",
        testo
    )

    return testo.strip()


def normalizza_titolo_google(titolo):

    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(titolo or "").lower()
    ).strip()


def punteggio_titolo_trama(titolo_cercato, titolo_trovato):

    cercato = normalizza_titolo_google(titolo_cercato)
    trovato = normalizza_titolo_google(titolo_trovato)

    if not cercato or not trovato:
        return 0

    if trovato == cercato:
        return 100

    if cercato in trovato:
        return 80

    if trovato in cercato:
        return 70

    parole_cercate = set(cercato.split())
    parole_trovate = set(trovato.split())

    if not parole_cercate:
        return 0

    comuni = len(parole_cercate & parole_trovate)
    rapporto = comuni / len(parole_cercate)

    if rapporto >= 0.75:
        return 55

    if rapporto >= 0.50:
        return 35

    return 0


def varianti_titolo_trama(titolo):

    titolo = str(titolo or "").strip()

    if not titolo:
        return []

    varianti = [titolo]

    base = re.sub(
        r"\s*(?:[-–—:]\s*)?(?:vol(?:ume)?\.?\s*)\d+\s*$",
        "",
        titolo,
        flags=re.IGNORECASE
    ).strip()

    if base and base not in varianti:
        varianti.append(base)

    base_parentesi = re.sub(
        r"\s*\(\s*vol(?:ume)?\.?\s*\d+\s*\)\s*$",
        "",
        titolo,
        flags=re.IGNORECASE
    ).strip()

    if base_parentesi and base_parentesi not in varianti:
        varianti.append(base_parentesi)

    return varianti


def sembra_trama_inglese(trama):

    testo = " " + re.sub(
        r"[^a-zA-ZÀ-ÿ']+",
        " ",
        str(trama or "").lower()
    ) + " "

    # Confronto semplice e prudente tra parole molto comuni.
    parole_inglesi = [
        " the ", " and ", " with ", " from ", " that ", " this ",
        " his ", " her ", " their ", " when ", " but ", " she ",
        " he ", " they ", " into ", " love ", " life ", " has ",
        " have ", " will ", " can ", " only ", " one "
    ]

    parole_italiane = [
        " che ", " con ", " per ", " non ", " una ", " uno ",
        " gli ", " delle ", " della ", " nella ", " quando ",
        " lui ", " lei ", " loro ", " amore ", " vita ", " ma ",
        " anche ", " come ", " suo ", " sua ", " sono "
    ]

    inglese = sum(
        testo.count(parola)
        for parola in parole_inglesi
    )

    italiano = sum(
        testo.count(parola)
        for parola in parole_italiane
    )

    return (
        (inglese >= 3 and inglese > italiano * 2)
        or
        (inglese >= 5 and italiano <= 2)
    )


EDIZIONI_ITALIANE_TRAMA = {
    "god of pain": {
        "isbn": "9788855318679",
        "titolo": "God of pain. Legacy of Gods. Ediz. italiana"
    }
}


def edizione_italiana_trama(titolo):
    chiave = normalizza_titolo_google(titolo)

    # Exact match first.
    if chiave in EDIZIONI_ITALIANE_TRAMA:
        return EDIZIONI_ITALIANE_TRAMA[chiave]

    # Also allow titles such as "God of Pain - Legacy of Gods".
    for nome, dati in EDIZIONI_ITALIANE_TRAMA.items():
        if chiave == nome or chiave.startswith(nome + " "):
            return dati

    return None


def cerca_trama_google_books(titolo):

    titolo = str(titolo or "").strip()

    if not titolo:
        return ""

    # Prima proviamo l'ISBN dell'edizione italiana quando lo conosciamo.
    # Questo evita che Google Books scelga l'edizione inglese dello stesso titolo.
    edizione = edizione_italiana_trama(titolo)

    if edizione:
        try:
            google_url_isbn = (
                "https://www.googleapis.com/books/v1/volumes"
                "?q=" + quote_plus("isbn:" + edizione["isbn"])
                + "&maxResults=10"
                + "&printType=books"
                + parametro_chiave_google()
            )

            dati_isbn = scarica_json(google_url_isbn)

            for item in dati_isbn.get("items", []):
                info = item.get("volumeInfo", {})
                descrizione = pulisci_trama_google(
                    info.get("description", "")
                )

                if descrizione and not sembra_trama_inglese(descrizione):
                    return descrizione

        except Exception as errore:
            print(
                "Ricerca trama tramite ISBN italiano non disponibile per",
                titolo,
                ":",
                errore,
                flush=True
            )

    # Cerchiamo SOLO edizioni italiane.
    # Per manga e serie proviamo anche il titolo senza "Vol. N".
    queries = []

    for variante in varianti_titolo_trama(titolo):
        queries.extend([
            'intitle:"' + variante + '"',
            variante,
            "intitle:" + variante,
        ])

    queries = list(dict.fromkeys(queries))
    candidati = []

    for query in queries:

        try:

            google_url = (
                "https://www.googleapis.com/books/v1/volumes"
                "?q=" + quote_plus(query)
                + "&maxResults=40"
                + "&printType=books"
                + "&orderBy=relevance"
                + "&langRestrict=it"
                + parametro_chiave_google()
            )

            dati = scarica_json(
                google_url
            )

            for item in dati.get(
                "items",
                []
            ):

                info = item.get(
                    "volumeInfo",
                    {}
                )

                lingua = str(
                    info.get(
                        "language",
                        ""
                    )
                ).lower().strip()

                # Non mostriamo descrizioni inglesi o di altre lingue.
                if lingua != "it":
                    continue

                descrizione = pulisci_trama_google(
                    info.get(
                        "description",
                        ""
                    )
                )

                if not descrizione:
                    continue

                # Alcune schede Google marcate come italiane contengono
                # comunque una descrizione inglese: non la usiamo.
                if sembra_trama_inglese(descrizione):
                    continue

                titolo_trovato = info.get(
                    "title",
                    ""
                )

                punteggio = punteggio_titolo_trama(
                    titolo,
                    titolo_trovato
                )

                # Evita di prendere la trama di un libro diverso.
                if punteggio <= 0:
                    continue

                candidati.append(
                    (
                        punteggio,
                        descrizione
                    )
                )

        except Exception as errore:

            print(
                "Ricerca trama italiana Google non disponibile per query",
                query,
                ":",
                errore,
                flush=True
            )

    if not candidati:
        return ""

    candidati.sort(
        key=lambda elemento: elemento[0],
        reverse=True
    )

    return candidati[0][1]


def cerca_trama_open_library(titolo):

    titolo = str(titolo or "").strip()

    if not titolo:
        return ""

    # Open Library non sempre espone una descrizione nella ricerca.
    # Cerchiamo le opere corrispondenti e poi leggiamo la scheda dell'opera.
    url = (
        "https://openlibrary.org/search.json"
        "?title=" + quote_plus(titolo)
        + "&limit=15"
        + "&fields=key,title"
    )

    try:
        dati = scarica_json(url)
    except Exception as errore:
        print(
            "Ricerca trama Open Library non disponibile:",
            errore,
            flush=True
        )
        return ""

    candidati = []

    for documento in dati.get("docs", []):

        chiave = str(
            documento.get("key", "")
        ).strip()

        titolo_trovato = str(
            documento.get("title", "")
        ).strip()

        punteggio = punteggio_titolo_trama(
            titolo,
            titolo_trovato
        )

        if not chiave or punteggio <= 0:
            continue

        try:

            opera = scarica_json(
                "https://openlibrary.org"
                + chiave
                + ".json"
            )

            descrizione = opera.get(
                "description",
                ""
            )

            if isinstance(descrizione, dict):
                descrizione = descrizione.get(
                    "value",
                    ""
                )

            descrizione = pulisci_trama_google(
                descrizione
            )

            if descrizione:
                candidati.append(
                    (
                        punteggio,
                        descrizione
                    )
                )

        except Exception:
            continue

    if not candidati:
        return ""

    candidati.sort(
        key=lambda elemento: elemento[0],
        reverse=True
    )

    return candidati[0][1]


TRAME_ITALIANE_FALLBACK = {
    # Romance: trame italiane di riserva per i titoli che Google Books non trova.
    "cinder ella": (
        "Ella ha diciotto anni e ama cinema e libri. Sul suo blog conosce Cinder, "
        "un ragazzo di cui ignora la vera identità, che diventa il suo migliore amico. "
        "Dopo un grave incidente in cui perde la madre e riporta profonde ustioni, Ella "
        "deve ricominciare da capo a Los Angeles, nella casa del padre che l'aveva "
        "abbandonata. Tra una nuova famiglia, difficoltà a scuola e il bisogno di ritrovare "
        "se stessa, decide di ricontattare Cinder, senza sapere che dietro quel nome si "
        "nasconde qualcuno molto più vicino al mondo di Hollywood di quanto immagini."
    ),
    "delay of game": (
        "John Whitman è il portiere dei Denver Pioneers e da anni è innamorato di Brooke "
        "Delgado, che invece sembra non sopportarlo. John non conosce il motivo del suo "
        "astio e vuole finalmente scoprire cosa si nasconde tra loro. Brooke, però, ha le "
        "sue ragioni per tenerlo a distanza e considera i giocatori di hockey arroganti e "
        "inaffidabili. Tra scontri, vecchi segreti e un'attrazione sempre più difficile da "
        "ignorare, i due saranno costretti ad affrontare ciò che li divide."
    ),
    "fireflies lullabies": (
        "Haley Rae Jackson arriva nella tranquilla Faraway Oaks con l'intenzione di tenere "
        "un profilo basso, lavorare e lasciarsi il passato alle spalle. La cittadina del "
        "Connecticut sembra il rifugio perfetto, lontano da Nashville e da ciò da cui sta "
        "scappando. I suoi piani cambiano quando torna in paese una celebre star del country, "
        "legata proprio al ranch dove Haley lavora in cambio di un alloggio. Un'estate, un "
        "patto e due vite segnate da scelte difficili potrebbero trasformare il loro futuro."
    ),
    "butcher blackbird": (
        "Sloane e Rowan condividono un segreto decisamente fuori dal comune: entrambi "
        "danno la caccia a persone pericolose che sono riuscite a sfuggire alla giustizia. "
        "Quando i loro percorsi si incrociano, tra rivalità, humour nero e un gioco sempre "
        "più rischioso nasce un'attrazione difficile da ignorare. Ma avvicinarsi significa "
        "anche mettere a nudo segreti capaci di trasformare la loro sfida in qualcosa di "
        "molto più personale."
    ),
    "deadly sins sloth": (
        "In una storia dark romance legata al peccato dell'accidia, desiderio, ossessione "
        "e zone d'ombra si intrecciano in un rapporto tutt'altro che semplice. I protagonisti "
        "si ritrovano coinvolti in un legame intenso e pericoloso, dove fidarsi dell'altra "
        "persona significa affrontare segreti, paure e conseguenze che possono cambiare "
        "completamente le loro vite."
    ),
    "le bugie che rubiamo": (
        "Tra segreti, bugie e un'attrazione che diventa sempre più difficile da controllare, "
        "i protagonisti si ritrovano intrappolati in un rapporto intenso e complicato. "
        "Quello che nasce tra loro mette alla prova fiducia e sentimenti, mentre le verità "
        "nascoste iniziano a venire a galla e ogni scelta rischia di avere conseguenze "
        "inaspettate."
    ),

    # Fallback verificato per l'edizione italiana di God of Pain.
    # È una sintesi originale, non una copia della scheda dell'editore.
    "god of pain": (
        "Annika Volkov, cresciuta in una potente famiglia mafiosa, sa che il suo futuro "
        "sembra già scritto. Eppure si sente attratta proprio dall'uomo da cui dovrebbe "
        "stare lontana: Creighton King, freddo e pericoloso, abituato alla violenza e ai "
        "combattimenti clandestini. Tra i due nasce un legame oscuro e irresistibile che "
        "li porta da sconosciuti ad amanti e poi a nemici, in una relazione segnata da "
        "desiderio, ossessione e conseguenze difficili da evitare."
    ),

    # Sinossi italiane di serie manga: vengono usate soltanto se Google Books
    # non dispone della trama italiana del singolo volume.
    "my dress up darling": (
        "Wakana Gojo è un liceale timido con una grande passione per le bambole Hina e "
        "per il cucito. Quando Marin Kitagawa, compagna di classe solare e appassionata "
        "di cosplay, scopre la sua abilità, gli chiede di aiutarla a realizzare i suoi "
        "costumi. Da questa collaborazione nasce un rapporto sempre più speciale."
    ),
    "my dress-up darling": (
        "Wakana Gojo è un liceale timido con una grande passione per le bambole Hina e "
        "per il cucito. Quando Marin Kitagawa, compagna di classe solare e appassionata "
        "di cosplay, scopre la sua abilità, gli chiede di aiutarla a realizzare i suoi "
        "costumi. Da questa collaborazione nasce un rapporto sempre più speciale."
    ),
    "i diari della speziale": (
        "Maomao, giovane esperta di erbe medicinali e veleni, viene portata alla corte "
        "imperiale come serva. Vorrebbe restare nell'ombra, ma le sue conoscenze la "
        "spingono a risolvere misteri e casi delicati del palazzo, attirando l'attenzione "
        "di Jinshi e coinvolgendola sempre di più negli intrighi di corte."
    ),
    "ice guy cool girl": (
        "Himuro discende da una donna delle nevi e, quando le sue emozioni prendono il "
        "sopravvento, provoca involontariamente fenomeni gelidi. Sul lavoro conosce "
        "Fuyutsuki, una collega calma e premurosa di cui si innamora. Tra piccoli gesti "
        "quotidiani e situazioni insolite, i due si avvicinano poco alla volta."
    ),
    "ice guy & cool girl": (
        "Himuro discende da una donna delle nevi e, quando le sue emozioni prendono il "
        "sopravvento, provoca involontariamente fenomeni gelidi. Sul lavoro conosce "
        "Fuyutsuki, una collega calma e premurosa di cui si innamora. Tra piccoli gesti "
        "quotidiani e situazioni insolite, i due si avvicinano poco alla volta."
    ),
    "a sign of affection": (
        "Yuki è una studentessa universitaria sorda che comunica soprattutto attraverso "
        "la lingua dei segni, i messaggi e la lettura labiale. L'incontro con Itsuomi, "
        "un ragazzo curioso del mondo e delle lingue, apre per lei nuovi orizzonti e dà "
        "inizio a una delicata storia d'amore."
    ),

    # Fantasy Romance · Da leggere
    "all this twisted glory": (
        "Alizeh, erede dei Jinn, è sempre più vicina a reclamare il futuro che le spetta. "
        "Cyrus, re di Tulan, le offre il proprio regno a una condizione terribile: sposarlo "
        "e poi ucciderlo. Tra intrighi, magia, desiderio e tradimenti, Alizeh dovrà capire "
        "di chi fidarsi mentre il suo destino e i suoi sentimenti diventano inseparabili."
    ),
    "belladonna": (
        "Signa Farrow è rimasta orfana da bambina e sembra essere perseguitata dalla morte, "
        "che continua a portarle via le persone che la accolgono. Trasferita a Thorn Grove, "
        "la dimora della famiglia Hawthorne, si ritrova coinvolta in un mistero di veleno, "
        "fantasmi e segreti, mentre il suo legame con la Morte diventa sempre più profondo."
    ),
    "bitten la regina dei lupi": (
        "Dopo l'attacco di un lupo mannaro, Vanessa Hart perde tutto e scopre di essere "
        "diventata a sua volta una licantropa. Costretta a entrare nella corte della Regina "
        "dei Lupi, cerca vendetta mentre impara a sopravvivere tra magia, intrighi e due "
        "affascinanti giovani che complicano i suoi piani."
    ),
    "caldo come il fuoco": (
        "Layla è metà demone e metà gargoyle e possiede un potere pericoloso: con un bacio "
        "può rubare l'anima. È innamorata di Zayne, il ragazzo con cui è cresciuta, ma non "
        "può avvicinarsi a lui. L'arrivo del misterioso demone Roth sconvolge ogni certezza "
        "e la trascina in un conflitto tra Guardiani, demoni e segreti sulla sua vera natura."
    ),
    "dark rise": (
        "Will, sedicenne in fuga dagli uomini che hanno ucciso sua madre, scopre che il "
        "mondo della magia non è scomparso come tutti credono. Accolto dagli ultimi custodi "
        "della Luce, viene trascinato in una guerra antica contro il ritorno del Re Oscuro, "
        "mentre verità sul suo destino minacciano di cambiare tutto ciò che crede di sapere."
    ),
    "dove bruciano gli oceani": (
        "In un mondo fantasy segnato da poteri pericolosi, rivalità e antichi segreti, una "
        "giovane protagonista viene trascinata in un conflitto più grande di lei. Tra "
        "alleanze instabili, nemici difficili da ignorare e un'attrazione che complica ogni "
        "scelta, dovrà decidere per cosa vale davvero la pena combattere."
    ),
    "dragonfall": (
        "In un mondo in cui i draghi sono stati cacciati e trasformati in leggenda, un drago "
        "assume forma umana per avvicinarsi a coloro che considera responsabili della rovina "
        "del suo popolo. Un incontro inatteso cambia però i suoi piani, dando vita a un legame "
        "pericoloso mentre ribellione e antichi poteri minacciano di esplodere."
    ),
    "half a soul": (
        "Theodora, detta Dora, ha perso metà della propria anima a causa della maledizione "
        "di una fata e da allora prova le emozioni in modo diverso dagli altri. Durante la "
        "stagione mondana londinese incontra Elias Wilder, il brusco Lord Sorcier, e con lui "
        "viene coinvolta in misteriosi eventi magici che potrebbero cambiare entrambi."
    ),
    "i peccati degli dei": (
        "Persefone vive nell'Olimpo moderno, dominato da famiglie potenti e intrighi politici. "
        "Quando scopre che vogliono costringerla a un matrimonio che non desidera, fugge nel "
        "territorio di Ade. Quello che nasce come un accordo tra due persone con obiettivi "
        "diversi si trasforma presto in un'attrazione capace di mettere in pericolo l'ordine dell'Olimpo."
    ),
    "il re degli elfi": (
        "Sommersa dai debiti, una giovane mezza elfa viene venduta e portata al castello del "
        "re degli elfi, dove diventa la sua assistente personale. Il suo compito è trovargli "
        "una moglie, ma mentre scopre un potere nascosto dentro di sé, il re propone un "
        "matrimonio di convenienza per proteggerla. La regola è semplice: non innamorarsi."
    ),
    "il re dei draghi": (
        "Quando il re dei draghi cerca una moglie capace di dargli un erede dotato di magia, "
        "una giovane mezzosangue viene convocata a Jade City nonostante creda di non avere "
        "abbastanza potere per essere scelta. Un segreto custodito da sua madre potrebbe però "
        "renderla molto più importante e molto più pericolosa di quanto immagini."
    ),
    "il trono di ghiaccio la lama dell assassina": (
        "Prima degli eventi de Il Trono di Ghiaccio, Celaena Sardothien è già una delle "
        "assassine più temute di Adarlan. Questa raccolta racconta le missioni, le ribellioni "
        "e il rapporto con Sam Cortland che la porteranno a sfidare il suo maestro Arobynn "
        "e a compiere scelte destinate a cambiare il suo futuro."
    ),
    "l immortale": (
        "Nella Russia del Novecento, Marja Morevna viene condotta in un mondo incantato da "
        "Koščej l'Immortale, zar della Vita. Tra prove imposte da Baba Jaga, una guerra contro "
        "lo zar della Morte e un amore tormentato, Marja attraversa magia e storia mentre "
        "cerca di comprendere il prezzo del legame che la unisce all'Immortale."
    ),
    "l incanto della biblioteca d agrifoglio": (
        "Kierse è una ladra che sopravvive in una New York dove esseri umani e mostri convivono "
        "grazie a una fragile tregua. Durante un furto entra nella biblioteca di Graves, un "
        "misterioso mostro che invece di ucciderla le offre un lavoro. L'addestramento che "
        "segue porta alla luce magia, segreti e un'attrazione sempre più difficile da ignorare."
    ),
    "la casa di terra e sangue": (
        "Bryce Quinlan conduce una vita spensierata finché un brutale omicidio distrugge il "
        "suo mondo. Quando gli omicidi ricominciano, viene coinvolta nelle indagini insieme "
        "all'angelo caduto Hunt Athalar. Tra magia, creature soprannaturali e segreti della "
        "città di Crescent City, i due scoprono una minaccia molto più grande del previsto."
    ),
    "la maschera di no": (
        "Nel Giappone del XVII secolo, Ichirō viene cresciuto tra le montagne da un samurai "
        "che gli insegna la via della spada. Dopo una tragedia che sconvolge la sua vita, "
        "raggiunge Edo e si avvicina al mondo del teatro kabuki, dove incontra nuovi amici "
        "e la misteriosa Hiinahime, una ragazza nascosta dietro una maschera del Nō."
    ),
    "little thieves": (
        "Vanja Schmidt è una ladra che ha rubato l'identità della principessa Gisele e usa "
        "il suo nuovo ruolo per derubare l'aristocrazia. Una maledizione, però, minaccia di "
        "trasformarla in gioielli se non restituisce ciò che ha preso. Per salvarsi dovrà "
        "affrontare segreti, inganni e un investigatore deciso a smascherarla."
    ),
    "mate": (
        "In un mondo in cui umani, vampiri e lupi mannari convivono in un equilibrio fragile, "
        "un legame inatteso unisce due persone che appartengono a fazioni diverse. Tra istinto, "
        "politica soprannaturale e un'attrazione impossibile da ignorare, i protagonisti "
        "dovranno capire se il loro rapporto può diventare qualcosa di più di una semplice alleanza."
    ),
    "quicksilver": (
        "Saeris Fane nasconde strani poteri e sopravvive rubando acqua in un regno crudele. "
        "Quando apre accidentalmente un portale, viene trascinata in una terra di Fae e "
        "costretta a collaborare con Kingfisher, un guerriero enigmatico e pericoloso. "
        "Tra alchimia, guerra e desiderio, Saeris scopre che il suo potere può cambiare due mondi."
    ),
    "rose in chains": (
        "In un regno segnato da guerra, magia e prigionia, una giovane donna si ritrova nelle "
        "mani del nemico e deve imparare a sopravvivere in un ambiente dove ogni alleanza ha "
        "un prezzo. Il rapporto con un uomo legato alla fazione opposta si trasforma lentamente "
        "in qualcosa di più complesso, tra desiderio, potere e scelte impossibili."
    ),
    "sun of blood and ruin": (
        "Nel Messico coloniale, Leonora de Las Casas conduce una doppia vita: nobildonna agli "
        "occhi della società e guerriera mascherata quando cala la notte. Dotata di poteri "
        "legati alle antiche divinità, combatte per proteggere il suo popolo mentre profezie, "
        "magia e un amore pericoloso la spingono verso una guerra inevitabile."
    ),
    "these infinite threads": (
        "Alizeh ha finalmente scoperto la verità sulle proprie origini, ma il suo futuro è "
        "più incerto che mai. Dopo gli eventi che hanno sconvolto Ardunia, si ritrova nelle "
        "mani del re Cyrus di Tulan, mentre Kamran cerca disperatamente di ritrovarla. "
        "Amore, vendetta e potere si intrecciano mentre antiche profezie cominciano a compiersi."
    ),
    "this woven kingdom": (
        "Alizeh vive nascosta come serva, ma in realtà è l'erede perduta di un antico popolo "
        "Jinn. Kamran, principe ereditario di Ardunia, dovrebbe temere la profezia che annuncia "
        "la caduta del suo regno, eppure resta irresistibilmente attratto da lei. Il loro "
        "incontro dà inizio a una storia di magia, intrighi e destini intrecciati."
    ),
    "thershing day": (
        "Una raccolta ambientata nel mondo della saga Empyrean che racconta tredici storie "
        "legate al giorno della Trebbiatura, il momento in cui cavalieri e draghi scelgono "
        "se legarsi. Le vicende seguono personaggi già conosciuti e mostrano da nuove "
        "prospettive incontri, legami e momenti decisivi della loro storia."
    ),
    "threshing day": (
        "Una raccolta ambientata nel mondo della saga Empyrean che racconta tredici storie "
        "legate al giorno della Trebbiatura, il momento in cui cavalieri e draghi scelgono "
        "se legarsi. Le vicende seguono personaggi già conosciuti e mostrano da nuove "
        "prospettive incontri, legami e momenti decisivi della loro storia."
    ),
    "una condanna di ombre e spine": (
        "Elise appartiene alla famiglia che un tempo sottrasse la corona ai Fae e ora viene "
        "costretta a un matrimonio per proteggere il trono e suo padre. Affidata a Legion Grey, "
        "scopre in lui un alleato tanto irritante quanto irresistibile. Quando un colpo di stato "
        "travolge il regno, Elise comprende che Legion custodisce segreti capaci di cambiare tutto."
    ),
    "una danza con il principe delle fate": (
        "Katria non crede nell'amore e accetta un matrimonio combinato soprattutto per sfuggire "
        "alla propria famiglia. Ma il suo nuovo marito non è un uomo qualunque: è legato al "
        "mondo dei Fae e a un antico rituale. Quando Katria ottiene involontariamente un potere "
        "che non le appartiene, viene trascinata in una corsa per la corona e per la propria libertà."
    ),
    "when the moon hatched": (
        "Raeve è un'assassina legata alla ribellione e vive in un mondo in cui i draghi morti "
        "diventano lune nel cielo. Dopo una perdita devastante viene catturata dal potere che "
        "combatte, mentre il guerriero Kaan cerca qualcuno che il mondo crede scomparso. "
        "Le loro strade si incrociano tra magia, ricordi perduti, vendetta e un legame antico."
    ),

}


def cerca_trama_fallback_italiana(titolo):
    normalizzato = normalizza_titolo_google(titolo)

    # Titoli esatti.
    for chiave, trama in TRAME_ITALIANE_FALLBACK.items():
        if normalizzato == normalizza_titolo_google(chiave):
            return trama

    # Serie manga: accetta anche "Vol. 6", "06", sottotitoli, ecc.
    for chiave, trama in TRAME_ITALIANE_FALLBACK.items():
        chiave_norm = normalizza_titolo_google(chiave)
        if chiave_norm and (
            normalizzato.startswith(chiave_norm + " ")
            or chiave_norm in normalizzato
        ):
            return trama

    return ""


def cerca_metadati_google_books(titolo):
    """Cerca insieme copertina e trama italiana della migliore edizione Google."""
    titolo = str(titolo or "").strip()
    if not titolo or not google_books_disponibile():
        return {}

    # Una sola query ben formata: evita 2-6 chiamate per lo stesso libro.
    query = 'intitle:"' + varianti_titolo_trama(titolo)[0] + '"'
    url = (
        "https://www.googleapis.com/books/v1/volumes"
        "?q=" + quote_plus(query)
        + "&maxResults=20&printType=books&orderBy=relevance"
        + parametro_chiave_google()
    )

    try:
        dati = scarica_json_google(url)
    except Exception as errore:
        print("Ricerca metadati Google non disponibile:", errore, flush=True)
        return {}

    if not dati:
        return {}

    candidati = []
    for item in dati.get("items", []):
        info = item.get("volumeInfo", {})
        trovato = str(info.get("title", "")).strip()
        if not trovato:
            continue

        punteggio = punteggio_titolo_trama(titolo, trovato)
        if punteggio <= 0:
            continue

        lingua = str(info.get("language", "")).lower().strip()
        if lingua == "it":
            punteggio += 80

        immagini = info.get("imageLinks", {}) or {}
        copertina = (
            immagini.get("extraLarge") or immagini.get("large")
            or immagini.get("medium") or immagini.get("small")
            or immagini.get("thumbnail") or immagini.get("smallThumbnail")
            or ""
        )
        if copertina:
            copertina = copertina.replace("http://", "https://")
            punteggio += 15

        trama = pulisci_trama_google(info.get("description", ""))
        if trama and not sembra_trama_inglese(trama):
            punteggio += 20
        else:
            trama = ""

        candidati.append((punteggio, {
            "titolo": trovato,
            "copertina": copertina,
            "trama": trama,
            "autori": info.get("authors", []) or [],
            "lingua": lingua,
            "fonte": "Google Books"
        }))

    if not candidati:
        return {}

    candidati.sort(key=lambda x: x[0], reverse=True)
    migliore = dict(candidati[0][1])

    for _, candidato in candidati[1:]:
        if not migliore.get("copertina") and candidato.get("copertina"):
            migliore["copertina"] = candidato["copertina"]
        if not migliore.get("trama") and candidato.get("trama"):
            migliore["trama"] = candidato["trama"]
        if migliore.get("copertina") and migliore.get("trama"):
            break

    return migliore


def cerca_internet_archive(titolo, autore="", isbn="", risultati=None, viste=None):
    """Terza fonte di riserva per copertine e metadati: Internet Archive."""
    titolo = str(titolo or "").strip()
    autore = str(autore or "").strip()
    isbn = str(isbn or "").strip()

    if not titolo and not isbn:
        return []

    parti = []
    if isbn:
        parti.append('isbn:"' + isbn.replace('"', '') + '"')
    elif titolo:
        parti.append('title:"' + titolo.replace('"', '') + '"')
        if autore:
            parti.append('creator:"' + autore.replace('"', '') + '"')

    query = " AND ".join(parti)
    url = (
        "https://archive.org/advancedsearch.php"
        "?q=" + quote_plus(query)
        + "&fl[]=identifier&fl[]=title&fl[]=creator&fl[]=year&fl[]=language"
        + "&rows=20&page=1&output=json"
    )

    try:
        dati = scarica_json(url)
    except Exception as errore:
        print("Internet Archive non disponibile:", errore, flush=True)
        return []

    trovati = []
    for doc in dati.get("response", {}).get("docs", []):
        identificatore = str(doc.get("identifier", "") or "").strip()
        titolo_trovato = str(doc.get("title", "") or "").strip()
        if not identificatore or not titolo_trovato:
            continue

        punteggio = punteggio_titolo_trama(titolo, titolo_trovato) if titolo else 60
        if titolo and punteggio <= 0:
            continue

        copertina = "https://archive.org/services/img/" + quote_plus(identificatore)
        creator = doc.get("creator", []) or []
        if isinstance(creator, str):
            creator = [creator]

        lingua = doc.get("language", "") or ""
        if isinstance(lingua, list):
            lingua_testo = str(lingua[0]) if lingua else ""
        else:
            lingua_testo = str(lingua)

        elemento = {
            "titolo": titolo_trovato,
            "autori": creator,
            "copertina": copertina,
            "fonte": "Internet Archive",
            "isbn": isbn,
            "editore": "",
            "anno": str(doc.get("year", "") or ""),
            "lingua": lingua_testo,
            "identifier": identificatore,
            "punteggio": punteggio,
        }
        trovati.append(elemento)

        if risultati is not None and viste is not None:
            aggiungi_risultato(
                risultati, viste, titolo_trovato, creator, copertina,
                "Internet Archive", isbn, "", elemento["anno"], lingua_testo
            )

    trovati.sort(key=lambda e: e.get("punteggio", 0), reverse=True)
    return trovati


def cerca_metadati_internet_archive(titolo):
    risultati = cerca_internet_archive(titolo)
    if not risultati:
        return {}
    migliore = risultati[0]
    return {
        "titolo": migliore.get("titolo", ""),
        "copertina": migliore.get("copertina", ""),
        "trama": "",
        "autori": migliore.get("autori", []) or [],
        "lingua": migliore.get("lingua", ""),
        "fonte": "Internet Archive",
    }


def cerca_trama_internet_archive(titolo):
    """Prova a leggere la descrizione dei migliori record Internet Archive."""
    risultati = cerca_internet_archive(titolo)
    for elemento in risultati[:3]:
        identificatore = elemento.get("identifier", "")
        if not identificatore:
            continue
        try:
            meta = scarica_json(
                "https://archive.org/metadata/" + quote_plus(identificatore)
            )
        except Exception:
            continue

        metadata = meta.get("metadata", {}) or {}
        descrizione = metadata.get("description", "") or ""
        if isinstance(descrizione, list):
            descrizione = " ".join(str(x) for x in descrizione if x)
        descrizione = pulisci_trama_google(descrizione)
        if descrizione and not sembra_trama_inglese(descrizione):
            return descrizione
    return ""


def cerca_metadati_open_library(titolo):
    """Fallback per copertina/metadati quando Google non risponde o è in 429."""
    titolo = str(titolo or "").strip()
    if not titolo:
        return {}

    url = (
        "https://openlibrary.org/search.json"
        "?title=" + quote_plus(titolo)
        + "&limit=20"
        + "&fields=key,title,author_name,cover_i,first_publish_year,isbn,language,publisher"
    )

    try:
        dati = scarica_json(url)
    except Exception as errore:
        print("Ricerca metadati Open Library non disponibile:", errore, flush=True)
        return {}

    candidati = []
    for doc in dati.get("docs", []):
        trovato = str(doc.get("title", "") or "").strip()
        punteggio = punteggio_titolo_trama(titolo, trovato)
        if punteggio <= 0:
            continue

        cover_id = doc.get("cover_i")
        copertina = (
            "https://covers.openlibrary.org/b/id/" + str(cover_id) + "-L.jpg"
            if cover_id else ""
        )
        if copertina:
            punteggio += 15

        lingue = doc.get("language", []) or []
        lingua = str(lingue[0]) if isinstance(lingue, list) and lingue else ""
        if "ita" in lingue or "it" in lingue:
            punteggio += 30

        candidati.append((punteggio, {
            "titolo": trovato,
            "copertina": copertina,
            "trama": "",
            "autori": doc.get("author_name", []) or [],
            "lingua": lingua,
            "fonte": "Open Library"
        }))

    if not candidati:
        return {}

    candidati.sort(key=lambda x: x[0], reverse=True)
    return dict(candidati[0][1])


def cerca_metadati_automatici(titolo):
    """Google API -> Google Books HTML -> Open Library -> Internet Archive."""
    metadati = cerca_metadati_google_books(titolo)
    if metadati.get("copertina") or metadati.get("trama"):
        return metadati

    html = cerca_google_books_html(titolo)
    if html.get("copertina") or html.get("trama"):
        return html

    ol = cerca_metadati_open_library(titolo)
    if ol.get("copertina") or ol.get("trama"):
        return ol

    return cerca_metadati_internet_archive(titolo)


def cerca_trama_automatica(titolo):
    titolo = str(titolo or "").strip()
    if not titolo:
        return ""

    # Le trame locali non consumano API e sono già in italiano.
    trama = cerca_trama_fallback_italiana(titolo)
    if trama:
        return trama

    # Una sola richiesta Google, solo se Google non è in pausa.
    metadati = cerca_metadati_google_books(titolo)
    trama = str(metadati.get("trama", "") or "").strip()
    if trama and not sembra_trama_inglese(trama):
        return trama

    # Seconda risorsa: pagina pubblica Google Books, senza API.
    html = cerca_google_books_html(titolo)
    trama = str(html.get("trama", "") or "").strip()
    if trama and not sembra_trama_inglese(trama):
        return trama

    # Terza risorsa: Open Library. Se la descrizione è inglese la scartiamo.
    trama = cerca_trama_open_library(titolo)
    if trama and not sembra_trama_inglese(trama):
        return trama

    # Quarta risorsa: Internet Archive.
    trama = cerca_trama_internet_archive(titolo)
    if trama and not sembra_trama_inglese(trama):
        return trama

    return ""


def correggi_trame_inglesi_salvate():

    try:

        with connessione_database() as conn:

            with conn.cursor() as cur:

                cur.execute("""
                    SELECT id, titolo, trama
                    FROM libri
                    WHERE COALESCE(TRIM(trama), '') <> ''
                    ORDER BY id ASC
                """)

                libri = cur.fetchall()

    except Exception as errore:

        print(
            "Impossibile controllare le vecchie trame:",
            errore,
            flush=True
        )
        return

    corrette = 0

    for id_libro, titolo, trama_attuale in libri:

        if not sembra_trama_inglese(
            trama_attuale
        ):
            continue

        try:

            trama_italiana = cerca_trama_automatica(
                titolo
            )

            # Se una vecchia trama è inglese, non la lasciamo visibile.
            # Se troviamo l'italiano la sostituiamo; altrimenti la svuotiamo.
            if not trama_italiana:
                with connessione_database() as conn:
                    with conn.cursor() as cur:
                        cur.execute("""
                            UPDATE libri
                            SET trama = ''
                            WHERE id = %s
                              AND trama = %s
                        """, (
                            id_libro,
                            trama_attuale
                        ))
                    conn.commit()

                print(
                    "🧹 Trama inglese rimossa (italiana non trovata):",
                    titolo,
                    flush=True
                )
                continue

            with connessione_database() as conn:

                with conn.cursor() as cur:

                    cur.execute("""
                        UPDATE libri
                        SET trama = %s
                        WHERE id = %s
                          AND trama = %s
                    """, (
                        trama_italiana,
                        id_libro,
                        trama_attuale
                    ))

                conn.commit()

            corrette += 1

            print(
                "🇮🇹 Trama inglese sostituita:",
                titolo,
                flush=True
            )

        except Exception as errore:

            print(
                "Errore correzione trama per",
                titolo,
                ":",
                errore,
                flush=True
            )

        time.sleep(0.15)

    print(
        "🇮🇹 Controllo vecchie trame completato. Corrette:",
        corrette,
        flush=True
    )


# ==========================================
# COMPLETA LE TRAME MANCANTI GIÀ NEL DATABASE
# ==========================================

def completa_trame_mancanti():

    try:

        with connessione_database() as conn:

            with conn.cursor() as cur:

                cur.execute("""
                    SELECT
                        id,
                        titolo
                    FROM libri
                    WHERE
                        COALESCE(TRIM(trama), '') = ''
                    ORDER BY
                        creato_il ASC,
                        id ASC
                """)

                libri_senza_trama = (
                    cur.fetchall()
                )

    except Exception as errore:

        print(
            "Impossibile controllare le trame mancanti:",
            errore,
            flush=True
        )

        return

    if not libri_senza_trama:

        print(
            "✅ Nessuna trama mancante nel database.",
            flush=True
        )

        return

    print(
        "📖 Trame mancanti da cercare:",
        len(libri_senza_trama),
        flush=True
    )

    trovate = 0

    for id_libro, titolo in libri_senza_trama:

        try:

            trama = cerca_trama_automatica(titolo
            )

            if trama:

                with connessione_database() as conn:

                    with conn.cursor() as cur:

                        cur.execute("""
                            UPDATE libri
                            SET
                                trama = %s
                            WHERE
                                id = %s
                                AND
                                COALESCE(TRIM(trama), '') = ''
                        """, (
                            trama,
                            id_libro
                        ))

                    conn.commit()

                trovate += 1

                print(
                    "✅ Trama trovata:",
                    titolo,
                    flush=True
                )

            else:

                print(
                    "⚠️ Trama non trovata:",
                    titolo,
                    flush=True
                )

        except Exception as errore:

            print(
                "Trama automatica non disponibile per",
                titolo,
                ":",
                errore,
                flush=True
            )

        time.sleep(0.15)

    print(
        "📚 Aggiornamento trame completato.",
        "Trovate:",
        trovate,
        "su",
        len(libri_senza_trama),
        flush=True
    )


def aggiungi_risultato(
    risultati,
    viste,
    titolo,
    autori,
    copertina,
    fonte,
    isbn="",
    editore="",
    anno="",
    lingua=""
):

    if not copertina:
        return

    copertina = str(
        copertina
    ).replace(
        "http://",
        "https://"
    )

    if copertina in viste:
        return

    viste.add(
        copertina
    )

    risultati.append({
        "titolo":
            titolo
            or
            "Senza titolo",

        "autori":
            autori
            if isinstance(
                autori,
                list
            )
            else [],

        "editore":
            editore
            or "",

        "anno":
            str(
                anno
                or
                ""
            ),

        "isbn":
            isbn
            or "",

        "lingua":
            lingua
            or "",

        "copertina":
            copertina,

        "fonte":
            fonte
    })


def cerca_google_books(
    titolo,
    autore,
    isbn,
    risultati,
    viste
):

    if isbn:

        query = (
            "isbn:"
            +
            isbn
        )

    else:

        query = titolo

        if autore:

            query += (
                " "
                +
                autore
            )

    google_url = (
        "https://www.googleapis.com/books/v1/volumes"
        "?q="
        +
        quote_plus(
            query
        )
        +
        "&maxResults=40"
        +
        "&printType=books"
        +
        parametro_chiave_google()
    )

    dati = scarica_json_google(google_url)
    if not dati:
        return

    for item in dati.get(
        "items",
        []
    ):

        info = item.get(
            "volumeInfo",
            {}
        )

        immagini = info.get(
            "imageLinks",
            {}
        )

        copertina = (
            immagini.get("extraLarge")
            or
            immagini.get("large")
            or
            immagini.get("medium")
            or
            immagini.get("small")
            or
            immagini.get("thumbnail")
            or
            immagini.get("smallThumbnail")
        )

        identificatori = info.get(
            "industryIdentifiers",
            []
        )

        isbn_trovato = ""

        for identificatore in identificatori:

            if (
                identificatore.get("type")
                ==
                "ISBN_13"
            ):

                isbn_trovato = (
                    identificatore.get(
                        "identifier",
                        ""
                    )
                )

                break

        aggiungi_risultato(
            risultati,
            viste,
            info.get(
                "title",
                ""
            ),
            info.get(
                "authors",
                []
            ),
            copertina,
            "Google Books",
            isbn_trovato,
            info.get(
                "publisher",
                ""
            ),
            info.get(
                "publishedDate",
                ""
            ),
            info.get(
                "language",
                ""
            )
        )


def cerca_open_library(
    titolo,
    autore,
    isbn,
    risultati,
    viste
):

    parametri = []

    if isbn:

        parametri.append(
            "isbn="
            +
            quote_plus(
                isbn
            )
        )

    elif titolo:

        parametri.append(
            "title="
            +
            quote_plus(
                titolo
            )
        )

        if autore:

            parametri.append(
                "author="
                +
                quote_plus(
                    autore
                )
            )

    parametri.append(
        "limit=40"
    )

    parametri.append(
        "fields=key,title,author_name,cover_i,first_publish_year,isbn,language,publisher"
    )

    open_library_url = (
        "https://openlibrary.org/search.json?"
        +
        "&".join(
            parametri
        )
    )

    dati = scarica_json(
        open_library_url
    )

    for documento in dati.get(
        "docs",
        []
    ):

        cover_id = documento.get(
            "cover_i"
        )

        if not cover_id:
            continue

        copertina = (
            "https://covers.openlibrary.org/b/id/"
            +
            str(
                cover_id
            )
            +
            "-L.jpg"
        )

        lista_isbn = documento.get(
            "isbn",
            []
        )

        isbn_trovato = ""

        if isinstance(
            lista_isbn,
            list
        ) and lista_isbn:

            isbn_trovato = str(
                lista_isbn[0]
            )

        editori = documento.get(
            "publisher",
            []
        )

        editore = ""

        if isinstance(
            editori,
            list
        ) and editori:

            editore = str(
                editori[0]
            )

        lingue = documento.get(
            "language",
            []
        )

        lingua = ""

        if isinstance(
            lingue,
            list
        ) and lingue:

            lingua = str(
                lingue[0]
            )

        aggiungi_risultato(
            risultati,
            viste,
            documento.get(
                "title",
                ""
            ),
            documento.get(
                "author_name",
                []
            ),
            copertina,
            "Open Library",
            isbn_trovato,
            editore,
            documento.get(
                "first_publish_year",
                ""
            ),
            lingua
        )


class LibreriaHandler(
    SimpleHTTPRequestHandler
):

    def leggi_json(self):

        lunghezza = int(
            self.headers.get(
                "Content-Length",
                "0"
            )
        )

        if lunghezza == 0:
            return {}

        contenuto = self.rfile.read(
            lunghezza
        )

        return json.loads(
            contenuto.decode(
                "utf-8"
            )
        )


    # ==========================================
    # CORS - RICHIESTE PREFLIGHT DEL BROWSER
    # ==========================================

    def do_OPTIONS(self):

        self.send_response(204)

        self.send_header(
            "Access-Control-Allow-Origin",
            "https://martina-virtual-library-static.onrender.com"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, PUT, DELETE, OPTIONS"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type"
        )

        self.send_header(
            "Access-Control-Max-Age",
            "86400"
        )

        self.end_headers()


    def do_GET(self):

        parsed = urlparse(
            self.path
        )

        if parsed.path == "/api/database-test":

            try:

                with connessione_database() as conn:

                    with conn.cursor() as cur:

                        cur.execute(
                            "SELECT 1"
                        )

                        risultato = (
                            cur.fetchone()
                        )

                self.invia_json({
                    "ok": True,
                    "database":
                        risultato[0]
                })

            except Exception as errore:

                print(
                    "Errore database:",
                    errore,
                    flush=True
                )

                self.invia_json({
                    "ok": False,
                    "errore":
                        "Connessione al database non riuscita."
                }, 500)

            return


        if parsed.path == "/api/libri":

            try:

                with connessione_database() as conn:

                    with conn.cursor() as cur:

                        cur.execute("""
                            SELECT
                                id,
                                titolo,
                                categoria,
                                stato,
                                copertina,
                                lettura_attuale,
                                preferito,
                                voto,
                                trama,
                                creato_il
                            FROM libri
                            ORDER BY
                                creato_il ASC,
                                id ASC
                        """)

                        righe = (
                            cur.fetchall()
                        )

                libri = [
                    libro_json(
                        riga
                    )
                    for riga in righe
                ]

                self.invia_json({
                    "ok": True,
                    "libri":
                        libri
                })

            except Exception as errore:

                print(
                    "Errore lettura libri:",
                    errore,
                    flush=True
                )

                self.invia_json({
                    "ok": False,
                    "errore":
                        "Impossibile caricare i libri."
                }, 500)

            return


        if parsed.path == "/api/trama-automatica":

            params = parse_qs(
                parsed.query
            )

            titolo = params.get(
                "titolo",
                [""]
            )[0].strip()

            if not titolo:

                self.invia_json({
                    "ok": False,
                    "errore":
                        "Scrivi il titolo del libro."
                }, 400)

                return

            try:

                trama = cerca_trama_automatica(titolo
                )

                # Ultimo controllo: l'endpoint non deve mai restituire inglese.
                if trama and sembra_trama_inglese(trama):
                    trama = ""

                self.invia_json({
                    "ok": True,
                    "titolo": titolo,
                    "trama": trama
                })

            except Exception as errore:

                print(
                    "Errore ricerca trama automatica:",
                    errore,
                    flush=True
                )

                self.invia_json({
                    "ok": False,
                    "errore":
                        "Impossibile cercare la trama."
                }, 500)

            return


        if parsed.path == "/api/copertine":

            params = parse_qs(
                parsed.query
            )

            titolo = params.get(
                "titolo",
                [""]
            )[0].strip()

            autore = params.get(
                "autore",
                [""]
            )[0].strip()

            isbn = params.get(
                "isbn",
                [""]
            )[0].strip()

            if not titolo and not isbn:

                self.invia_json({
                    "errore":
                        "Scrivi almeno il titolo oppure l'ISBN."
                }, 400)

                return

            risultati = []
            viste = set()

            try:

                try:

                    cerca_google_books(
                        titolo,
                        autore,
                        isbn,
                        risultati,
                        viste
                    )

                except Exception as errore:

                    print(
                        "Google Books non disponibile:",
                        errore,
                        flush=True
                    )

                try:
                    html_meta = cerca_google_books_html(titolo)
                    if html_meta.get("copertina"):
                        aggiungi_risultato(
                            risultati, viste,
                            html_meta.get("titolo", titolo),
                            html_meta.get("autori", []),
                            html_meta.get("copertina", ""),
                            "Google Books HTML"
                        )
                except Exception as errore:
                    print("Google Books HTML non disponibile:", errore, flush=True)

                try:

                    cerca_open_library(
                        titolo,
                        autore,
                        isbn,
                        risultati,
                        viste
                    )

                except Exception as errore:

                    print(
                        "Open Library non disponibile:",
                        errore,
                        flush=True
                    )

                try:
                    cerca_internet_archive(
                        titolo, autore, isbn, risultati, viste
                    )
                except Exception as errore:
                    print(
                        "Internet Archive non disponibile:",
                        errore,
                        flush=True
                    )

                self.invia_json({
                    "risultati":
                        risultati[:60]
                })

            except Exception as errore:

                print(
                    "Errore ricerca copertine:",
                    errore,
                    flush=True
                )

                self.invia_json({
                    "errore":
                        "Errore nella ricerca delle copertine."
                }, 500)

            return

        super().do_GET()


    def do_POST(self):

        parsed = urlparse(
            self.path
        )

        if parsed.path == "/api/libri":

            try:

                dati = self.leggi_json()

                titolo = str(
                    dati.get(
                        "titolo",
                        ""
                    )
                ).strip()

                categoria = str(
                    dati.get(
                        "categoria",
                        ""
                    )
                ).strip()

                stato = str(
                    dati.get(
                        "stato",
                        ""
                    )
                ).strip()

                copertina = str(
                    dati.get(
                        "copertina",
                        ""
                    )
                ).strip()

                lettura_attuale = bool(
                    dati.get(
                        "lettura_attuale",
                        False
                    )
                )

                preferito_inviato = (
                    "preferito"
                    in dati
                )

                preferito = bool(
                    dati.get(
                        "preferito",
                        False
                    )
                )

                metadati_automatici = {}
                try:
                    metadati_automatici = cerca_metadati_automatici(titolo)
                except Exception as errore:
                    print("Metadati automatici non disponibili:", errore, flush=True)

                if not copertina:
                    copertina = str(
                        metadati_automatici.get("copertina", "") or ""
                    ).strip()

                if not titolo:

                    self.invia_json({
                        "ok": False,
                        "errore":
                            "Il titolo è obbligatorio."
                    }, 400)

                    return

                if not categoria:

                    self.invia_json({
                        "ok": False,
                        "errore":
                            "La categoria è obbligatoria."
                    }, 400)

                    return

                if stato not in [
                    "Letto",
                    "Da leggere"
                ]:

                    self.invia_json({
                        "ok": False,
                        "errore":
                            "Stato non valido."
                    }, 400)

                    return

                with connessione_database() as conn:

                    with conn.cursor() as cur:

                        if lettura_attuale:

                            cur.execute("""
                                UPDATE libri
                                SET
                                    lettura_attuale = FALSE
                            """)

                        cur.execute("""
                            SELECT
                                id,
                                trama
                            FROM libri
                            WHERE
                                LOWER(TRIM(titolo))
                                =
                                LOWER(TRIM(%s))
                            LIMIT 1
                        """, (
                            titolo,
                        ))

                        esistente = (
                            cur.fetchone()
                        )

                        trama_automatica = str(
                            metadati_automatici.get("trama", "") or ""
                        ).strip()

                        trama_esistente = (
                            str(
                                esistente[1] or ""
                            ).strip()
                            if esistente
                            else ""
                        )

                        if not trama_esistente and not trama_automatica:

                            try:

                                trama_automatica = (
                                    cerca_trama_automatica(titolo
                                    )
                                )

                            except Exception as errore:

                                print(
                                    "Trama automatica non disponibile:",
                                    errore,
                                    flush=True
                                )

                                trama_automatica = ""

                        if esistente:

                            if preferito_inviato:

                                cur.execute("""
                                    UPDATE libri
                                    SET
                                        titolo = %s,
                                        categoria = %s,
                                        stato = %s,
                                        copertina = %s,
                                        lettura_attuale = %s,
                                        preferito = %s,
                                        trama = CASE
                                            WHEN COALESCE(TRIM(trama), '') = ''
                                            THEN %s
                                            ELSE trama
                                        END
                                    WHERE id = %s
                                    RETURNING
                                        id,
                                        titolo,
                                        categoria,
                                        stato,
                                        copertina,
                                        lettura_attuale,
                                        preferito,
                                        voto,
                                        trama,
                                        creato_il
                                """, (
                                    titolo,
                                    categoria,
                                    stato,
                                    copertina,
                                    lettura_attuale,
                                    preferito,
                                    trama_automatica,
                                    esistente[0]
                                ))

                            else:

                                cur.execute("""
                                    UPDATE libri
                                    SET
                                        titolo = %s,
                                        categoria = %s,
                                        stato = %s,
                                        copertina = %s,
                                        lettura_attuale = %s,
                                        trama = CASE
                                            WHEN COALESCE(TRIM(trama), '') = ''
                                            THEN %s
                                            ELSE trama
                                        END
                                    WHERE id = %s
                                    RETURNING
                                        id,
                                        titolo,
                                        categoria,
                                        stato,
                                        copertina,
                                        lettura_attuale,
                                        preferito,
                                        voto,
                                        trama,
                                        creato_il
                                """, (
                                    titolo,
                                    categoria,
                                    stato,
                                    copertina,
                                    lettura_attuale,
                                    trama_automatica,
                                    esistente[0]
                                ))

                        else:

                            cur.execute("""
                                INSERT INTO libri (
                                    titolo,
                                    categoria,
                                    stato,
                                    copertina,
                                    lettura_attuale,
                                    preferito,
                                    trama
                                )
                                VALUES (
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s,
                                    %s
                                )
                                RETURNING
                                    id,
                                    titolo,
                                    categoria,
                                    stato,
                                    copertina,
                                    lettura_attuale,
                                    preferito,
                                    voto,
                                    trama,
                                    creato_il
                            """, (
                                titolo,
                                categoria,
                                stato,
                                copertina,
                                lettura_attuale,
                                preferito,
                                trama_automatica
                            ))

                        riga = (
                            cur.fetchone()
                        )

                    conn.commit()

                self.invia_json({
                    "ok": True,
                    "libro":
                        libro_json(
                            riga
                        )
                }, 201)

            except Exception as errore:

                print(
                    "Errore aggiunta libro:",
                    errore,
                    flush=True
                )

                self.invia_json({
                    "ok": False,
                    "errore":
                        "Impossibile salvare il libro."
                }, 500)

            return


        if parsed.path.startswith(
            "/api/preferito/"
        ):

            try:

                id_libro = int(
                    parsed.path.split(
                        "/"
                    )[-1]
                )

                dati = self.leggi_json()

                preferito = bool(
                    dati.get(
                        "preferito",
                        False
                    )
                )

                with connessione_database() as conn:

                    with conn.cursor() as cur:

                        cur.execute("""
                            UPDATE libri
                            SET
                                preferito = %s
                            WHERE id = %s
                            RETURNING
                                id,
                                titolo,
                                categoria,
                                stato,
                                copertina,
                                lettura_attuale,
                                preferito,
                                voto,
                                trama,
                                creato_il
                        """, (
                            preferito,
                            id_libro
                        ))

                        riga = (
                            cur.fetchone()
                        )

                        if not riga:

                            self.invia_json({
                                "ok": False,
                                "errore":
                                    "Libro non trovato."
                            }, 404)

                            return

                    conn.commit()

                self.invia_json({
                    "ok": True,
                    "libro":
                        libro_json(
                            riga
                        )
                })

            except Exception as errore:

                print(
                    "Errore preferito:",
                    errore,
                    flush=True
                )

                self.invia_json({
                    "ok": False,
                    "errore":
                        "Impossibile aggiornare il preferito."
                }, 500)

            return


        if parsed.path.startswith(
            "/api/voto/"
        ):

            try:

                id_libro = int(
                    parsed.path.split(
                        "/"
                    )[-1]
                )

                dati = self.leggi_json()

                voto = dati.get(
                    "voto",
                    None
                )

                if voto in [
                    "",
                    0,
                    "0",
                    None
                ]:

                    voto = None

                else:

                    try:

                        voto = int(
                            voto
                        )

                    except (
                        TypeError,
                        ValueError
                    ):

                        self.invia_json({
                            "ok": False,
                            "errore":
                                "Il voto deve essere da 1 a 5 stelle."
                        }, 400)

                        return

                    if voto not in [
                        1,
                        2,
                        3,
                        4,
                        5
                    ]:

                        self.invia_json({
                            "ok": False,
                            "errore":
                                "Il voto deve essere da 1 a 5 stelle."
                        }, 400)

                        return

                with connessione_database() as conn:

                    with conn.cursor() as cur:

                        cur.execute("""
                            UPDATE libri
                            SET
                                voto = %s
                            WHERE id = %s
                            RETURNING
                                id,
                                titolo,
                                categoria,
                                stato,
                                copertina,
                                lettura_attuale,
                                preferito,
                                voto,
                                trama,
                                creato_il
                        """, (
                            voto,
                            id_libro
                        ))

                        riga = (
                            cur.fetchone()
                        )

                        if not riga:

                            self.invia_json({
                                "ok": False,
                                "errore":
                                    "Libro non trovato."
                            }, 404)

                            return

                    conn.commit()

                self.invia_json({
                    "ok": True,
                    "libro":
                        libro_json(
                            riga
                        )
                })

            except Exception as errore:

                print(
                    "Errore voto:",
                    errore,
                    flush=True
                )

                self.invia_json({
                    "ok": False,
                    "errore":
                        "Impossibile aggiornare il voto."
                }, 500)

            return


        if parsed.path.startswith(
            "/api/trama/"
        ):

            try:

                id_libro = int(
                    parsed.path.split(
                        "/"
                    )[-1]
                )

                dati = self.leggi_json()

                trama = str(
                    dati.get(
                        "trama",
                        ""
                    )
                ).strip()

                with connessione_database() as conn:

                    with conn.cursor() as cur:

                        cur.execute("""
                            UPDATE libri
                            SET
                                trama = %s
                            WHERE id = %s
                            RETURNING
                                id,
                                titolo,
                                categoria,
                                stato,
                                copertina,
                                lettura_attuale,
                                preferito,
                                voto,
                                trama,
                                creato_il
                        """, (
                            trama,
                            id_libro
                        ))

                        riga = (
                            cur.fetchone()
                        )

                        if not riga:

                            self.invia_json({
                                "ok": False,
                                "errore":
                                    "Libro non trovato."
                            }, 404)

                            return

                    conn.commit()

                self.invia_json({
                    "ok": True,
                    "libro":
                        libro_json(
                            riga
                        )
                })

            except Exception as errore:

                print(
                    "Errore trama:",
                    errore,
                    flush=True
                )

                self.invia_json({
                    "ok": False,
                    "errore":
                        "Impossibile aggiornare la trama."
                }, 500)

            return


        if parsed.path.startswith(
            "/api/lettura-attuale/"
        ):

            try:

                id_libro = int(
                    parsed.path.split(
                        "/"
                    )[-1]
                )

                with connessione_database() as conn:

                    with conn.cursor() as cur:

                        cur.execute("""
                            UPDATE libri
                            SET
                                lettura_attuale = FALSE
                        """)

                        cur.execute("""
                            UPDATE libri
                            SET
                                lettura_attuale = TRUE,
                                stato = 'Da leggere'
                            WHERE id = %s
                            RETURNING
                                id,
                                titolo,
                                categoria,
                                stato,
                                copertina,
                                lettura_attuale,
                                preferito,
                                voto,
                                trama,
                                creato_il
                        """, (
                            id_libro,
                        ))

                        riga = (
                            cur.fetchone()
                        )

                        if not riga:

                            self.invia_json({
                                "ok": False,
                                "errore":
                                    "Libro non trovato."
                            }, 404)

                            return

                    conn.commit()

                self.invia_json({
                    "ok": True,
                    "libro":
                        libro_json(
                            riga
                        )
                })

            except Exception as errore:

                print(
                    "Errore lettura attuale:",
                    errore,
                    flush=True
                )

                self.invia_json({
                    "ok": False,
                    "errore":
                        "Impossibile impostare la lettura."
                }, 500)

            return


        if parsed.path.startswith(
            "/api/termina-lettura/"
        ):

            try:

                id_libro = int(
                    parsed.path.split(
                        "/"
                    )[-1]
                )

                with connessione_database() as conn:

                    with conn.cursor() as cur:

                        cur.execute("""
                            UPDATE libri
                            SET
                                lettura_attuale = FALSE,
                                stato = 'Letto'
                            WHERE id = %s
                            RETURNING
                                id,
                                titolo,
                                categoria,
                                stato,
                                copertina,
                                lettura_attuale,
                                preferito,
                                voto,
                                trama,
                                creato_il
                        """, (
                            id_libro,
                        ))

                        riga = (
                            cur.fetchone()
                        )

                        if not riga:

                            self.invia_json({
                                "ok": False,
                                "errore":
                                    "Libro non trovato."
                            }, 404)

                            return

                    conn.commit()

                self.invia_json({
                    "ok": True,
                    "libro":
                        libro_json(
                            riga
                        )
                })

            except Exception as errore:

                print(
                    "Errore termina lettura:",
                    errore,
                    flush=True
                )

                self.invia_json({
                    "ok": False,
                    "errore":
                        "Impossibile terminare la lettura."
                }, 500)

            return

        self.invia_json({
            "ok": False,
            "errore":
                "Indirizzo non trovato."
        }, 404)


    def do_PUT(self):

        parsed = urlparse(
            self.path
        )

        if parsed.path.startswith(
            "/api/libri/"
        ):

            try:

                id_libro = int(
                    parsed.path.split(
                        "/"
                    )[-1]
                )

                dati = self.leggi_json()

                titolo = str(
                    dati.get(
                        "titolo",
                        ""
                    )
                ).strip()

                categoria = str(
                    dati.get(
                        "categoria",
                        ""
                    )
                ).strip()

                stato = str(
                    dati.get(
                        "stato",
                        ""
                    )
                ).strip()

                copertina = str(
                    dati.get(
                        "copertina",
                        ""
                    )
                ).strip()

                lettura_attuale = bool(
                    dati.get(
                        "lettura_attuale",
                        False
                    )
                )

                preferito_inviato = (
                    "preferito"
                    in dati
                )

                preferito = bool(
                    dati.get(
                        "preferito",
                        False
                    )
                )

                if not titolo:

                    self.invia_json({
                        "ok": False,
                        "errore":
                            "Il titolo è obbligatorio."
                    }, 400)

                    return

                if stato not in [
                    "Letto",
                    "Da leggere"
                ]:

                    self.invia_json({
                        "ok": False,
                        "errore":
                            "Stato non valido."
                    }, 400)

                    return

                with connessione_database() as conn:

                    with conn.cursor() as cur:

                        if lettura_attuale:

                            cur.execute("""
                                UPDATE libri
                                SET
                                    lettura_attuale = FALSE
                            """)

                        if preferito_inviato:

                            cur.execute("""
                                UPDATE libri
                                SET
                                    titolo = %s,
                                    categoria = %s,
                                    stato = %s,
                                    copertina = %s,
                                    lettura_attuale = %s,
                                    preferito = %s
                                WHERE id = %s
                                RETURNING
                                    id,
                                    titolo,
                                    categoria,
                                    stato,
                                    copertina,
                                    lettura_attuale,
                                    preferito,
                                    voto,
                                    trama,
                                    creato_il
                            """, (
                                titolo,
                                categoria,
                                stato,
                                copertina,
                                lettura_attuale,
                                preferito,
                                id_libro
                            ))

                        else:

                            cur.execute("""
                                UPDATE libri
                                SET
                                    titolo = %s,
                                    categoria = %s,
                                    stato = %s,
                                    copertina = %s,
                                    lettura_attuale = %s
                                WHERE id = %s
                                RETURNING
                                    id,
                                    titolo,
                                    categoria,
                                    stato,
                                    copertina,
                                    lettura_attuale,
                                    preferito,
                                    voto,
                                    trama,
                                    creato_il
                            """, (
                                titolo,
                                categoria,
                                stato,
                                copertina,
                                lettura_attuale,
                                id_libro
                            ))

                        riga = (
                            cur.fetchone()
                        )

                        if not riga:

                            self.invia_json({
                                "ok": False,
                                "errore":
                                    "Libro non trovato."
                            }, 404)

                            return

                    conn.commit()

                self.invia_json({
                    "ok": True,
                    "libro":
                        libro_json(
                            riga
                        )
                })

            except Exception as errore:

                print(
                    "Errore modifica libro:",
                    errore,
                    flush=True
                )

                self.invia_json({
                    "ok": False,
                    "errore":
                        "Impossibile modificare il libro."
                }, 500)

            return

        self.invia_json({
            "ok": False,
            "errore":
                "Indirizzo non trovato."
        }, 404)


    def do_DELETE(self):

        parsed = urlparse(
            self.path
        )

        if parsed.path.startswith(
            "/api/libri/"
        ):

            try:

                id_libro = int(
                    parsed.path.split(
                        "/"
                    )[-1]
                )

                with connessione_database() as conn:

                    with conn.cursor() as cur:

                        cur.execute("""
                            DELETE FROM libri
                            WHERE id = %s
                            RETURNING id
                        """, (
                            id_libro,
                        ))

                        eliminato = (
                            cur.fetchone()
                        )

                        if not eliminato:

                            self.invia_json({
                                "ok": False,
                                "errore":
                                    "Libro non trovato."
                            }, 404)

                            return

                    conn.commit()

                self.invia_json({
                    "ok": True,
                    "id":
                        id_libro
                })

            except Exception as errore:

                print(
                    "Errore eliminazione libro:",
                    errore,
                    flush=True
                )

                self.invia_json({
                    "ok": False,
                    "errore":
                        "Impossibile eliminare il libro."
                }, 500)

            return

        self.invia_json({
            "ok": False,
            "errore":
                "Indirizzo non trovato."
        }, 404)


    def invia_json(
        self,
        dati,
        codice=200
    ):

        contenuto = json.dumps(
            dati,
            ensure_ascii=False
        ).encode(
            "utf-8"
        )

        self.send_response(
            codice
        )

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(
                len(
                    contenuto
                )
            )
        )

        self.send_header(
            "Cache-Control",
            "no-store"
        )

        self.send_header(
            "Access-Control-Allow-Origin",
            "https://martina-virtual-library-static.onrender.com"
        )

        self.send_header(
            "Access-Control-Allow-Methods",
            "GET, POST, PUT, DELETE, OPTIONS"
        )

        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type"
        )

        self.end_headers()

        self.wfile.write(
            contenuto
        )


server = ThreadingHTTPServer(
    (
        "0.0.0.0",
        PORT
    ),
    LibreriaHandler
)


print(
    "📚 Martina's Virtual Library"
)


if GOOGLE_BOOKS_API_KEY:

    print(
        "✅ Google Books API attiva"
    )

else:

    print(
        "ℹ️ Google Books senza chiave; Open Library è il fallback principale in caso di 429"
    )


print(
    "✅ Open Library attiva"
)


if DATABASE_URL:

    print(
        "✅ DATABASE_URL trovata"
    )

else:

    print(
        "⚠️ DATABASE_URL non trovata"
    )


print(
    "Apri: http://localhost:"
    +
    str(
        PORT
    )
)


print(
    "Per fermare il server: Ctrl+C"
)


def aggiorna_trame_all_avvio():
    correggi_trame_inglesi_salvate()
    completa_trame_mancanti()


# IMPORTANTE: non avviare una scansione massiva a ogni deploy di Render.
# Le trame vengono cercate quando aggiungi/salvi il singolo libro.
# La scansione automatica causava troppe richieste a Google Books (HTTP 429).
# threading.Thread(
#     target=aggiorna_trame_all_avvio,
#     daemon=True
# ).start()


try:

    server.serve_forever()

except KeyboardInterrupt:

    print(
        "\nServer chiuso."
    )

    server.server_close()
