"""Sert l'écran de démo en local : python serve.py  (puis http://127.0.0.1:8795/)"""
import functools, http.server, os
ICI = os.path.dirname(os.path.abspath(__file__))
Gestion = functools.partial(http.server.SimpleHTTPRequestHandler, directory=ICI)
print("Écran : http://127.0.0.1:8795/  (Entrée pour entrer, flèches pour avancer, Échap pour remonter)")
http.server.ThreadingHTTPServer(("127.0.0.1", 8795), Gestion).serve_forever()
