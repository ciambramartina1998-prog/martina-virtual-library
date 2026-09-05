import os
import json

from urllib.parse import urlparse, parse_qs, quote_plus
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

import psycopg


PORT = int(os.environ.get("PORT", "8000"))

# =========================================================
# GOOGLE BOOKS
# =========================================================

GOOGLE_BOOKS_API_KEY = "AIzaSyBcoCAZjqAbSYWOasvy93iODPPIe1LtJkE"


# =========================================================
# NEON DATABASE
# =========================================================

DATABASE_URL = os.environ.get("DATABASE_URL")


def connessione_database():
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL non configurato.")

    return psycopg.connect(DATABASE_URL)


def libro_json(riga):
    return {
        "id": riga[0],
        "titolo": riga[1],
        "categoria": riga[2],
        "stato": riga[3],
        "copertina": riga[4],
        "lettura_attuale": riga[5],
        "creato_il": (
            riga[6].isoformat()
            if riga[6]
            else None
        )
    }


class LibreriaHandler(SimpleHTTPRequestHandler):

    # =====================================================
    # LETTURA BODY JSON
    # =====================================================

    def leggi_json(self):
        lunghezza = int(
            self.headers.get("Content-Length", "0")
        )

        if lunghezza == 0:
            return {}

        contenuto = self.rfile.read(lunghezza)

        return json.loads(
            contenuto.decode("utf-8")
        )

    # =====================================================
    # GET
    # =====================================================

    def do_GET(self):

        parsed = urlparse(self.path)

        # -------------------------------------------------
        # TEST DATABASE
        # -------------------------------------------------

        if parsed.path == "/api/database-test":

            try:

                with connessione_database() as conn:
                    with conn.cursor() as cur:

                        cur.execute("SELECT 1")

                        risultato = cur.fetchone()

                self.invia_json({
                    "ok": True,
                    "database": risultato[0]
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

        # -------------------------------------------------
        # ELENCO LIBRI
        # -------------------------------------------------

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
                                creato_il
                            FROM libri
                            ORDER BY creato_il ASC, id ASC
                        """)

                        righe = cur.fetchall()

                libri = [
                    libro_json(riga)
                    for riga in righe
                ]

                self.invia_json({
                    "ok": True,
                    "libri": libri
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

        # -------------------------------------------------
        # GOOGLE BOOKS
        # -------------------------------------------------

        if parsed.path == "/api/copertine":

            params = parse_qs(parsed.query)

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

            try:

                if isbn:

                    query = "isbn:" + isbn

                else:

                    query = titolo

                    if autore:
                        query += " " + autore

                google_url = (
                    "https://www.googleapis.com/books/v1/volumes"
                    "?q=" + quote_plus(query)
                    + "&maxResults=40"
                    + "&printType=books"
                    + "&key="
                    + quote_plus(
                        GOOGLE_BOOKS_API_KEY
                    )
                )

                richiesta = Request(
                    google_url,
                    headers={
                        "User-Agent":
                        "Mozilla/5.0",
                        "Accept":
                        "application/json"
                    }
                )

                with urlopen(
                    richiesta,
                    timeout=20
                ) as risposta:

                    dati = json.loads(
                        risposta.read().decode(
                            "utf-8"
                        )
                    )

                risultati = []

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
                        or immagini.get("large")
                        or immagini.get("medium")
                        or immagini.get("small")
                        or immagini.get("thumbnail")
                        or immagini.get("smallThumbnail")
                    )

                    if not copertina:
                        continue

                    copertina = copertina.replace(
                        "http://",
                        "https://"
                    )

                    autori = info.get(
                        "authors",
                        []
                    )

                    identificatori = info.get(
                        "industryIdentifiers",
                        []
                    )

                    isbn_trovato = ""

                    for ident in identificatori:

                        if (
                            ident.get("type")
                            == "ISBN_13"
                        ):

                            isbn_trovato = ident.get(
                                "identifier",
                                ""
                            )

                            break

                    risultati.append({
                        "titolo":
                        info.get(
                            "title",
                            "Senza titolo"
                        ),

                        "autori":
                        autori,

                        "editore":
                        info.get(
                            "publisher",
                            ""
                        ),

                        "anno":
                        info.get(
                            "publishedDate",
                            ""
                        ),

                        "isbn":
                        isbn_trovato,

                        "lingua":
                        info.get(
                            "language",
                            ""
                        ),

                        "copertina":
                        copertina,

                        "fonte":
                        "Google Books"
                    })

                unici = []
                viste = set()

                for libro in risultati:

                    url = libro[
                        "copertina"
                    ]

                    if url in viste:
                        continue

                    viste.add(url)

                    unici.append(libro)

                self.invia_json({
                    "risultati":
                    unici[:30]
                })

            except HTTPError as errore:

                print(
                    "Google Books errore:",
                    errore.code,
                    flush=True
                )

                self.invia_json({
                    "errore":
                    "Google Books ha restituito errore "
                    + str(errore.code)
                }, 500)

            except URLError as errore:

                print(
                    "Errore connessione:",
                    errore,
                    flush=True
                )

                self.invia_json({
                    "errore":
                    "Problema di connessione a Google Books."
                }, 500)

            except Exception as errore:

                print(
                    "Errore Google Books:",
                    errore,
                    flush=True
                )

                self.invia_json({
                    "errore":
                    "Errore nella ricerca."
                }, 500)

            return

        # -------------------------------------------------
        # FILE NORMALI DEL SITO
        # -------------------------------------------------

        super().do_GET()

    # =====================================================
    # POST
    # =====================================================

    def do_POST(self):

        parsed = urlparse(self.path)

        # -------------------------------------------------
        # AGGIUNGI LIBRO
        # -------------------------------------------------

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

                        # Se diventa lettura attuale,
                        # rimuove la lettura attuale
                        # dagli altri libri.

                        if lettura_attuale:

                            cur.execute("""
                                UPDATE libri
                                SET lettura_attuale = FALSE
                            """)

                        # Controlla se esiste già
                        # un libro con lo stesso titolo.

                        cur.execute("""
                            SELECT id
                            FROM libri
                            WHERE LOWER(TRIM(titolo))
                            =
                            LOWER(TRIM(%s))
                            LIMIT 1
                        """, (
                            titolo,
                        ))

                        esistente = cur.fetchone()

                        if esistente:

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
                                    creato_il
                            """, (
                                titolo,
                                categoria,
                                stato,
                                copertina,
                                lettura_attuale,
                                esistente[0]
                            ))

                        else:

                            cur.execute("""
                                INSERT INTO libri (
                                    titolo,
                                    categoria,
                                    stato,
                                    copertina,
                                    lettura_attuale
                                )
                                VALUES (
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
                                    creato_il
                            """, (
                                titolo,
                                categoria,
                                stato,
                                copertina,
                                lettura_attuale
                            ))

                        riga = cur.fetchone()

                    conn.commit()

                self.invia_json({
                    "ok": True,
                    "libro":
                    libro_json(riga)
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

        # -------------------------------------------------
        # IMPOSTA STO LEGGENDO
        # -------------------------------------------------

        if parsed.path.startswith(
            "/api/lettura-attuale/"
        ):

            try:

                id_libro = int(
                    parsed.path.split("/")[-1]
                )

                with connessione_database() as conn:

                    with conn.cursor() as cur:

                        cur.execute("""
                            UPDATE libri
                            SET lettura_attuale = FALSE
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
                                creato_il
                        """, (
                            id_libro,
                        ))

                        riga = cur.fetchone()

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
                    libro_json(riga)
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

        # -------------------------------------------------
        # TERMINA LETTURA
        # -------------------------------------------------

        if parsed.path.startswith(
            "/api/termina-lettura/"
        ):

            try:

                id_libro = int(
                    parsed.path.split("/")[-1]
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
                                creato_il
                        """, (
                            id_libro,
                        ))

                        riga = cur.fetchone()

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
                    libro_json(riga)
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

    # =====================================================
    # PUT - MODIFICA LIBRO
    # =====================================================

    def do_PUT(self):

        parsed = urlparse(self.path)

        if parsed.path.startswith(
            "/api/libri/"
        ):

            try:

                id_libro = int(
                    parsed.path.split("/")[-1]
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
                                SET lettura_attuale = FALSE
                            """)

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
                                creato_il
                        """, (
                            titolo,
                            categoria,
                            stato,
                            copertina,
                            lettura_attuale,
                            id_libro
                        ))

                        riga = cur.fetchone()

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
                    libro_json(riga)
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

    # =====================================================
    # DELETE - ELIMINA LIBRO
    # =====================================================

    def do_DELETE(self):

        parsed = urlparse(self.path)

        if parsed.path.startswith(
            "/api/libri/"
        ):

            try:

                id_libro = int(
                    parsed.path.split("/")[-1]
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

                        eliminato = cur.fetchone()

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
                    "id": id_libro
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

    # =====================================================
    # RISPOSTA JSON
    # =====================================================

    def invia_json(
        self,
        dati,
        codice=200
    ):

        contenuto = json.dumps(
            dati,
            ensure_ascii=False
        ).encode("utf-8")

        self.send_response(codice)

        self.send_header(
            "Content-Type",
            "application/json; charset=utf-8"
        )

        self.send_header(
            "Content-Length",
            str(len(contenuto))
        )

        self.send_header(
            "Cache-Control",
            "no-store"
        )

        self.end_headers()

        self.wfile.write(contenuto)


# =========================================================
# AVVIO SERVER
# =========================================================

server = ThreadingHTTPServer(
    ("0.0.0.0", PORT),
    LibreriaHandler
)

print(
    "📚 Martina's Virtual Library"
)

print(
    "✅ Google Books API attiva"
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
    + str(PORT)
)

print(
    "Per fermare il server: Ctrl+C"
)


try:

    server.serve_forever()

except KeyboardInterrupt:

    print(
        "\nServer chiuso."
    )

    server.server_close()