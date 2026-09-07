import os
import json

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
        "creato_il": (
            riga[6].isoformat()
            if riga[6]
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
            immagini.get(
                "extraLarge"
            )
            or
            immagini.get(
                "large"
            )
            or
            immagini.get(
                "medium"
            )
            or
            immagini.get(
                "small"
            )
            or
            immagini.get(
                "thumbnail"
            )
            or
            immagini.get(
                "smallThumbnail"
            )
        )


        identificatori = info.get(
            "industryIdentifiers",
            []
        )

        isbn_trovato = ""

        for identificatore in identificatori:

            if (
                identificatore.get(
                    "type"
                )
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
                            SELECT id
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


try:

    server.serve_forever()

except KeyboardInterrupt:

    print(
        "\nServer chiuso."
    )


    server.server_close()
