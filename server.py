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


def cerca_trama_google_books(titolo):

    if not GOOGLE_BOOKS_API_KEY:
        return ""

    titolo = str(titolo or "").strip()

    if not titolo:
        return ""

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
                + "&key=" + quote_plus(GOOGLE_BOOKS_API_KEY)
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


def cerca_trama_automatica(titolo):

    # Mostriamo solo trame italiane.
    # Se Google Books non ha una descrizione italiana,
    # lasciamo la trama vuota invece di mostrare quella inglese.
    return cerca_trama_google_books(
        titolo
    )


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

    if not GOOGLE_BOOKS_API_KEY:
        return

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
        "&key="
        +
        quote_plus(
            GOOGLE_BOOKS_API_KEY
        )
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

                        trama_automatica = ""

                        trama_esistente = (
                            str(
                                esistente[1] or ""
                            ).strip()
                            if esistente
                            else ""
                        )

                        if not trama_esistente:

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
        "⚠️ GOOGLE_BOOKS_API_KEY non trovata"
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


threading.Thread(
    target=aggiorna_trame_all_avvio,
    daemon=True
).start()


try:

    server.serve_forever()

except KeyboardInterrupt:

    print(
        "\nServer chiuso."
    )

    server.server_close()
