#!/usr/bin/env python3
"""
War_ning – actualiza data/levels.json con los avisos de viaje del
Departamento de Estado de EE.UU. (feed oficial https://travel.state.gov/_res/rss/TAsTWs.xml).

Principios:
  * No inventa niveles: copia el nivel oficial (1-4), su etiqueta y la fecha de publicación.
  * Ante cualquier anomalía estructural NO sobrescribe el archivo y termina con error
    (así el workflow falla y GitHub avisa; la app sigue mostrando los últimos datos buenos).
  * Lo que no puede interpretar con certeza (nivel ausente, fecha inválida, enlace
    a otro dominio, nivel contradictorio) se omite y se informa en el log.

Códigos de salida: 0 = correcto (con o sin cambios) · 1 = anomalía, no se escribió nada · 2 = fallo de descarga.
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

from defusedxml import ElementTree as ET   # bloquea entidades XML maliciosas (XXE, billion laughs)
import pycountry

FEED_URL = "https://travel.state.gov/_res/rss/TAsTWs.xml"
ALLOWED_HOST = "travel.state.gov"
USER_AGENT = "War_ning-data-bot/1.0 (proyecto educativo PanaMentorLabs)"
SOURCE_ID = "us-state"
SOURCE_NAME = "Departamento de Estado (EE.UU.)"
SOURCE_URL = "https://travel.state.gov/content/travel/en/traveladvisories/traveladvisories.html"
MAX_BYTES = 50 * 1024 * 1024
DEFAULT_MIN_ITEMS = 150        # el feed real trae ~200; por debajo de esto algo va mal
MAX_DROP_RATIO = 0.15          # no se acepta perder >15 % de países respecto a la corrida anterior

# Traducción de los 4 niveles estándar (el texto original se conserva en etiqueta_original)
LABELS_ES = {1: "Precauciones normales", 2: "Mayor precaución", 3: "Reconsiderar el viaje", 4: "No viajar"}

# Nombres que pycountry no reconoce tal cual (se comparan ya normalizados)
ALIASES = {
    "burma": "MM", "turkey": "TR", "russia": "RU", "brunei": "BN", "micronesia": "FM",
    "democratic republic of the congo": "CD", "kosovo": "XK", "macau": "MO", "cape verde": "CV",
    "vatican city": "VA", "holy see": "VA", "kyrgyz republic": "KG", "slovak republic": "SK",
}


# ---------------------------------------------------------------- utilidades
def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c)).lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"^the ", "", s)


def build_iso_index() -> dict:
    idx = {}
    for c in pycountry.countries:
        for attr in ("name", "official_name", "common_name"):
            v = getattr(c, attr, None)
            if v:
                idx.setdefault(norm_name(v), c.alpha_2)
    idx.update(ALIASES)
    return idx


def resolve_iso(name: str, idx: dict):
    """Coincidencia EXACTA de nombre. Si no hay certeza, devuelve None (nunca adivina)."""
    candidates = [name]
    m = re.match(r"^(.*?)\s*\((.*?)\)\s*$", name)          # "Burma (Myanmar)" -> "Burma", "Myanmar"
    if m:
        candidates += [m.group(1), m.group(2)]
    for cand in candidates:
        iso = idx.get(norm_name(cand))
        if iso:
            return iso
    return None


def slug_from_link(link: str) -> str:
    last = link.rstrip("/").split("/")[-1].lower()
    last = re.sub(r"\.html?$", "", last)
    m = re.match(r"^(?P<s>[a-z0-9-]+?)(?:-travel)?-advisory\d*$", last)
    return m.group("s") if m else last


def name_from_slug(slug: str) -> str:
    return " ".join(w.capitalize() for w in slug.split("-"))


def parse_date(s: str):
    """El feed usa 'Tue, 08 Sep 2026' (sin hora)."""
    for fmt in ("%a, %d %b %Y", "%a, %d %b %Y %H:%M:%S %Z", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


def fetch(url: str, retries: int = 3) -> bytes:
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5"})
            with urllib.request.urlopen(req, timeout=30) as r:
                final_host = urllib.parse.urlparse(r.geturl()).hostname
                if final_host != ALLOWED_HOST:
                    raise RuntimeError(f"Redirección a un dominio no permitido: {final_host}")
                data = r.read(MAX_BYTES + 1)
                if len(data) > MAX_BYTES:
                    raise RuntimeError("Respuesta demasiado grande")
                return data
        except Exception as e:                      # noqa: BLE001
            last = e
            print(f"Intento {attempt}/{retries} falló: {e}", file=sys.stderr)
            time.sleep(2 * attempt)
    raise RuntimeError(f"No se pudo descargar el feed: {last}")


# ---------------------------------------------------------------- parseo
def parse_feed(xml_bytes: bytes, iso_idx: dict):
    """Devuelve (paises_por_id, anomalias, omitidos, sin_iso)."""
    root = ET.fromstring(xml_bytes)
    if root.tag != "rss":
        raise ValueError(f"Raíz inesperada: <{root.tag}>")
    channel = root.find("channel")
    if channel is None or "travel advisories" not in (channel.findtext("title") or "").lower():
        raise ValueError("El canal no parece ser el feed de Travel Advisories")

    candidates = {}
    anomalies, skipped, no_iso = [], [], []

    for item in channel.findall("item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()

        if not link.startswith(f"https://{ALLOWED_HOST}/"):
            anomalies.append(f"Enlace fuera de {ALLOWED_HOST}, omitido: {title!r}")
            continue

        level_txt = next((c.text or "" for c in item.findall("category") if c.get("domain") == "Threat-Level"), "")
        m_cat = re.search(r"Level\s*([1-4])\b", level_txt)
        m_tit = re.search(r"Level\s*([1-4])\b", title)
        if not m_cat and not m_tit:
            skipped.append(f"Sin nivel 1-4 (p. ej. alertas mundiales): {title!r}")
            continue
        if m_cat and m_tit and m_cat.group(1) != m_tit.group(1):
            anomalies.append(f"Nivel contradictorio entre título y categoría, omitido: {title!r}")
            continue
        nivel = int((m_cat or m_tit).group(1))
        original = re.sub(r"^\s*Level\s*[1-4]\s*:?\s*", "", level_txt).strip() if m_cat else ""

        fecha = parse_date(item.findtext("pubDate") or "")
        if not fecha:
            anomalies.append(f"Fecha inválida, omitido: {title!r}")
            continue

        slug = slug_from_link(link)
        composite = bool(re.search(r"see (individual )?summar", title, re.I))
        name = name_from_slug(slug) if composite else title.split(" - ")[0].strip()
        if not name:
            anomalies.append(f"Sin nombre, omitido: {link}")
            continue
        if composite:
            skipped.append(f"Entrada compuesta, nombre tomado de la URL ({name}): {title!r}")

        iso = resolve_iso(name, iso_idx)
        if not iso:
            no_iso.append(name)
        cid = iso.lower() if iso else slug

        entry = {
            "id": cid,
            "iso": iso,
            "nombre": name,
            "niveles": [{
                "fuente": SOURCE_NAME,
                "escala": "1-4", "min": 1, "max": 4,
                "nivel": nivel,
                "etiqueta": LABELS_ES[nivel],
                "etiqueta_original": original,
                "fecha": fecha,
                "url": link,
            }],
        }
        # Duplicados: se prefiere la entrada no compuesta y, luego, la más reciente
        rank = (not composite, fecha)
        if cid not in candidates or rank > candidates[cid][0]:
            candidates[cid] = (rank, entry)

    return {k: v[1] for k, v in candidates.items()}, anomalies, skipped, no_iso


# ---------------------------------------------------------------- comparación
def levels_map(data: dict) -> dict:
    out = {}
    for p in data.get("paises", []):
        key = p.get("id") or p.get("iso")
        for n in p.get("niveles", []):
            out[(key, n.get("fuente"))] = (p.get("nombre"), n.get("nivel"))
    return out


def diff_levels(prev: dict, new: dict):
    a, b = levels_map(prev), levels_map(new)
    changes = []
    for k, (name, lvl) in sorted(b.items(), key=lambda kv: kv[1][0] or ""):
        if k not in a:
            changes.append(f"{name}: nuevo (nivel {lvl})")
        elif a[k][1] != lvl:
            changes.append(f"{name}: nivel {a[k][1]} → {lvl}")
    for k, (name, lvl) in a.items():
        if k not in b:
            changes.append(f"{name}: eliminado (era nivel {lvl})")
    return changes


def write_atomic(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
            f.write("\n")
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# ---------------------------------------------------------------- principal
def main() -> int:
    root = Path(__file__).resolve().parent.parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", help="Leer el feed de un archivo local (pruebas) en vez de descargarlo")
    ap.add_argument("--output", default=str(root / "data" / "levels.json"))
    ap.add_argument("--min-items", type=int, default=DEFAULT_MIN_ITEMS)
    args = ap.parse_args()
    out_path = Path(args.output)

    try:
        xml_bytes = Path(args.input).read_bytes() if args.input else fetch(FEED_URL)
    except Exception as e:                          # noqa: BLE001
        print(f"ERROR de descarga: {e}", file=sys.stderr)
        return 2

    try:
        paises, anomalies, skipped, no_iso = parse_feed(xml_bytes, build_iso_index())
    except Exception as e:                          # noqa: BLE001
        print(f"ERROR: feed inválido, no se escribió nada: {e}", file=sys.stderr)
        return 1

    for line in anomalies:
        print("ANOMALÍA:", line, file=sys.stderr)
    for line in skipped:
        print("Omitido:", line)
    if no_iso:
        print(f"Sin código ISO (se mantiene el nombre en inglés): {sorted(set(no_iso))}")

    prev = {}
    if out_path.exists():
        try:
            prev = json.loads(out_path.read_text(encoding="utf-8"))
        except Exception:                           # noqa: BLE001
            prev = {}
    prev_real = bool(prev) and not prev.get("ejemplo", False)

    n = len(paises)
    if n < args.min_items:
        print(f"ERROR: solo {n} avisos válidos (mínimo {args.min_items}). No se escribió nada.", file=sys.stderr)
        return 1
    if prev_real:
        prev_n = len(prev.get("paises", []))
        if prev_n and n < prev_n * (1 - MAX_DROP_RATIO):
            print(f"ERROR: bajaron de {prev_n} a {n} países (>{int(MAX_DROP_RATIO*100)} %). No se escribió nada.", file=sys.stderr)
            return 1

    today = dt.datetime.now(dt.timezone.utc).date().isoformat()
    new = {
        "generado": today,
        "ejemplo": False,
        "fuentes": [{"id": SOURCE_ID, "nombre": SOURCE_NAME, "url": SOURCE_URL,
                     "nota": "Contenido oficial publicado por el Departamento de Estado de EE.UU. (travel.state.gov)."}],
        "paises": sorted(paises.values(), key=lambda p: p["nombre"].casefold()),
    }

    changes = diff_levels(prev, new) if prev_real else []
    same_core = prev_real and prev.get("paises") == new["paises"] and prev.get("fuentes") == new["fuentes"]
    if same_core and prev.get("generado") == today:
        print(f"Sin cambios ({n} países).")
        return 0

    write_atomic(out_path, new)
    print(f"Escrito {out_path} con {n} países. Cambios de nivel: {len(changes)}")
    for c in changes[:40]:
        print("  •", c)

    msg_file = os.environ.get("COMMIT_MSG_FILE")
    if msg_file:
        if changes:
            body = "\n".join(f"- {c}" for c in changes[:30]) + (f"\n… y {len(changes)-30} más" if len(changes) > 30 else "")
            msg = f"Datos: {len(changes)} cambio(s) en avisos del Dpto. de Estado ({today})\n\n{body}\n"
        else:
            msg = f"Datos: verificado sin cambios de nivel ({today})\n"
        Path(msg_file).write_text(msg, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
