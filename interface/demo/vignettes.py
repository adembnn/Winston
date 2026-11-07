"""Images réelles d'une enquête pour l'écran : python vignettes.py <enquete> [--base http://127.0.0.1:8802]

Pour chaque pièce de l'export : la miniature des vidéos (yt-dlp, téléchargée dans captures/),
sinon l'image d'aperçu de la page source (og:image). Écrit vignettes-<enquete>.json (pièce -> image).
Données générées : jamais commitées (voir .gitignore)."""
import argparse, concurrent.futures as cf, json, os, re, subprocess, urllib.parse, urllib.request

ICI = os.path.dirname(os.path.abspath(__file__))
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130 Safari/537.36"}

def lire(u, n=400000):
    return urllib.request.urlopen(urllib.request.Request(u, headers=UA), timeout=10).read(n)

def url_de(p):
    return (p.get("attributs") or {}).get("url") or ((p.get("preuves") or [{}])[0]).get("url")

def miniature(p):
    u = url_de(p)
    if not u:
        return p["id"], None
    try:
        if p["type"] == "video":
            r = subprocess.run(["yt-dlp", "--no-warnings", "--skip-download", "--print", "thumbnail", u],
                               capture_output=True, text=True, timeout=60)
            t = [l for l in (r.stdout or "").splitlines() if l.startswith("http")]
            if not t:
                return p["id"], None
            os.makedirs(os.path.join(ICI, "captures"), exist_ok=True)
            brut, jpg = os.path.join(ICI, "captures", p["id"] + ".img"), os.path.join(ICI, "captures", p["id"] + ".jpg")
            open(brut, "wb").write(lire(t[-1], 10_000_000))
            subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", brut, "-vf", "scale='min(720,iw)':-2", "-q:v", "4", jpg], timeout=30)
            os.remove(brut)
            return p["id"], "captures/" + p["id"] + ".jpg" if os.path.exists(jpg) else None
        if u.lower().endswith(".pdf"):
            return p["id"], None
        h = lire(u).decode("utf-8", "ignore")
        m = (re.search(r'<meta[^>]+(?:property|name)=["\'](?:og:image|twitter:image)["\'][^>]+content=["\']([^"\']+)', h)
             or re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name)=["\'](?:og:image|twitter:image)', h))
        return p["id"], m and urllib.parse.urljoin(u, m.group(1).replace("&amp;", "&"))
    except Exception:
        return p["id"], None

if __name__ == "__main__":
    a = argparse.ArgumentParser(); a.add_argument("enquete"); a.add_argument("--base", default="http://127.0.0.1:8802")
    o = a.parse_args()
    d = json.load(urllib.request.urlopen(f"{o.base}/api/export?enquete={o.enquete}", timeout=10))
    pieces = [p for x in d["dossiers"] for p in x.get("pieces", [])]
    with cf.ThreadPoolExecutor(6) as ex:
        res = {k: v for k, v in ex.map(miniature, pieces) if v}
    json.dump(res, open(os.path.join(ICI, f"vignettes-{o.enquete}.json"), "w"), indent=1)
    print(f"{len(res)} images sur {len(pieces)} pièces")
