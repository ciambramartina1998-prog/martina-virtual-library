from urllib.parse import urlparse, parse_qs, quote_plus
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote_plus
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import json

PORT = 8000

# 🔑 INCOLLA QUI LA TUA CHIAVE GOOGLE BOOKS
GOOGLE_BOOKS_API_KEY = "AIzaSyBcoCAZjqAbSYWOasvy93iODPPIe1LtJkE"


class LibreriaHandler(SimpleHTTPRequestHandler):

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/api/copertine":
            params = parse_qs(parsed.query)

            titolo = params.get("titolo", [""])[0].strip()
            autore = params.get("autore", [""])[0].strip()
            isbn = params.get("isbn", [""])[0].strip()

            if not titolo and not isbn:
                self.invia_json(
                    {"errore": "Scrivi almeno il titolo oppure l'ISBN."},
                    400
                )
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
                    + "&key=" + quote_plus(GOOGLE_BOOKS_API_KEY)
                )

                richiesta = Request(
                    google_url,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Accept": "application/json"
                    }
                )

                with urlopen(richiesta, timeout=20) as risposta:
                    dati = json.loads(
                        risposta.read().decode("utf-8")
                    )

                risultati = []

                for item in dati.get("items", []):
                    info = item.get("volumeInfo", {})
                    immagini = info.get("imageLinks", {})

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

                    autori = info.get("authors", [])
                    identificatori = info.get(
                        "industryIdentifiers",
                        []
                    )

                    isbn_trovato = ""

                    for ident in identificatori:
                        if ident.get("type") == "ISBN_13":
                            isbn_trovato = ident.get(
                                "identifier",
                                ""
                            )
                            break

                    risultati.append({
                        "titolo": info.get(
                            "title",
                            "Senza titolo"
                        ),
                        "autori": autori,
                        "editore": info.get(
                            "publisher",
                            ""
                        ),
                        "anno": info.get(
                            "publishedDate",
                            ""
                        ),
                        "isbn": isbn_trovato,
                        "lingua": info.get(
                            "language",
                            ""
                        ),
                        "copertina": copertina,
                        "fonte": "Google Books"
                    })

                unici = []
                viste = set()

                for libro in risultati:
                    url = libro["copertina"]

                    if url in viste:
                        continue

                    viste.add(url)
                    unici.append(libro)

                self.invia_json({
                    "risultati": unici[:30]
                })

            except HTTPError as errore:
                try:
                    dettaglio = errore.read().decode("utf-8")
                except:
                    dettaglio = ""

                print(
                    "Google Books errore:",
                    errore.code,
                    dettaglio
                )

                self.invia_json({
                    "errore":
                    "Google Books ha restituito errore "
                    + str(errore.code)
                }, 500)

            except URLError as errore:
                print(
                    "Errore connessione:",
                    errore
                )

                self.invia_json({
                    "errore":
                    "Problema di connessione a Google Books."
                }, 500)

            except Exception as errore:
                print(
                    "Errore:",
                    errore
                )

                self.invia_json({
                    "errore":
                    "Errore nella ricerca: "
                    + str(errore)
                }, 500)

            return

        super().do_GET()

    def invia_json(self, dati, codice=200):
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

        self.end_headers()

        self.wfile.write(contenuto)


server = ThreadingHTTPServer(
    ("0.0.0.0", PORT),
    LibreriaHandler
)

print("📚 Martina's Virtual Library")
print("✅ Google Books API attiva")
print("Apri: http://localhost:8000")
print("Per fermare il server: Ctrl+C")

try:
    server.serve_forever()

except KeyboardInterrupt:
    print("\nServer chiuso.")
    server.server_close()
