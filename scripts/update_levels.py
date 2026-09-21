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

Campos opcionales añadidos (la app funciona igual sin ellos):
  * raíz "cambios": [{id, iso, nombre, fuente, de, a, fecha}] (máx. 60 días, 300 registros)
  * raíz "historial_desde": fecha de inicio del historial
  * por entrada: "nivel_anterior" + "cambio" (fecha ISO del día de la detección)

Códigos de salida: 0 = correcto (con o sin cambios) · 1 = anomalía, no se escribió nada · 2 = fallo de descarga.
"""
import argparse
import datetime as dt
import html as htmllib
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
FETCH_ROUNDS = 4              # el feed devuelve conjuntos distintos entre peticiones: se pide varias veces y se unen
FETCH_PAUSE_S = 4
CAMBIOS_MAX_AGE = 60         # días que se conserva una marca de cambio (entrada y historial)
CAMBIOS_KEEP = 300           # máximo de registros en el historial raíz "cambios"
RETAIN_DAYS = 30             # cuánto se conserva un país de nivel 3-4 que desaparece del feed
MAX_DROP_RATIO = 0.15          # no se acepta perder >15 % de países respecto a la corrida anterior

# Ciclo oficial de revisión del Departamento de Estado: niveles 1-2 al menos cada 12 meses,
# niveles 3-4 al menos cada 6 meses. Pasado ese plazo, el aviso figura como "sin revisión reciente".
REVIEW_DAYS = {1: 365, 2: 365, 3: 183, 4: 183}

# Motivos: se buscan SOLO en la frase principal del aviso ("... due to <motivos>"), con un vocabulario
# cerrado. Lo que no reconoce se ignora: nunca se inventa un motivo.
MOTIVOS = [
    ("conflicto_armado", r"armed conflict|\bwar\b|hostilities|\binvasion\b"),
    ("terrorismo", r"terroris"),
    ("crimen", r"\bcrimes?\b|\bcriminal"),
    ("disturbios", r"\bunrest\b"),
    ("secuestro", r"kidnap|hostage"),
    ("detencion_injusta", r"wrongful detention"),
    ("salud", r"\bhealth\b|\bdisease|\boutbreak|\bepidemic|\bpandemic|\bebola\b|\bmarburg\b|\bcholera\b|\bmeasles\b|\bdengue\b|\bmalaria\b|\bmpox\b|\bzika\b"),
    ("desastres_naturales", r"natural disaster|hurricane|earthquake|volcan|cyclone|typhoon|tsunami|flood|wildfire"),
    ("minas", r"landmine|unexploded ordnance"),
    ("eventos_limitados", r"time-limited event|limited-time event"),
]
MOTIVOS_RE = [(k, re.compile(p, re.I)) for k, p in MOTIVOS]
HEAD_RE = re.compile(
    r"(?:do not travel|reconsider travel|exercise increased caution|exercise normal precautions|exercise caution)\b"
    r"([^.]{0,140}?)\bdue to\b\s*(:?)\s*", re.I)
END_LIST_RE = re.compile(r"read the entire|advisory summary|country summary|reissued|\blevel [1-4]\b", re.I)

# Países que sin duda tienen aviso de nivel alto. Si el feed no los trae, se avisa en el registro
# (el feed oficial ha mostrado omisiones y cambios de formato).
CANARIOS = {"ML": "Mali", "KP": "North Korea", "IR": "Iran", "IQ": "Iraq", "UA": "Ukraine", "RU": "Russia",
            "SY": "Syria", "SD": "Sudan", "YE": "Yemen", "AF": "Afghanistan", "LB": "Lebanon", "LY": "Libya",
            "SS": "South Sudan", "CF": "Central African Republic", "SO": "Somalia", "HT": "Haiti",
            "BF": "Burkina Faso", "NE": "Niger", "MM": "Burma", "BY": "Belarus"}

# Entradas agrupadas del feed: cuando aparece la entrada conjunta, se eliminan sus componentes
# de nivel 1-2 (la agrupada los resume). Fácil de ampliar: nombre normalizado -> componentes normalizados.
GRUPOS_TERRITORIOS = {
    "saba and sint eustatius": ["saba", "sint eustatius"],
    "french west indies": ["guadeloupe", "martinique", "saint barthelemy"],
}

# Traducción de los 4 niveles estándar (el texto original se conserva en etiqueta_original)
LABELS_ES = {1: "Precauciones normales", 2: "Mayor precaución", 3: "Reconsiderar el viaje", 4: "No viajar"}

SNIPPETS = {}   # id -> primeros caracteres del texto del aviso (solo para el registro)

# Nombres que pycountry no reconoce tal cual (se comparan ya normalizados)
ALIASES = {
    "burma": "MM", "turkey": "TR", "russia": "RU", "brunei": "BN", "micronesia": "FM",
    "democratic republic of the congo": "CD", "kosovo": "XK", "macau": "MO", "cape verde": "CV",
    "cote d ivoire": "CI", "sint maarten": "SX", "vatican city": "VA", "holy see": "VA", "kyrgyz republic": "KG", "slovak republic": "SK",
}


# ---------------------------------------------------------------- utilidades
def norm_name(s: str) -> str:
    s = s.replace("\u2019", "'").replace("\u2018", "'").replace("\u02bc", "'")   # apóstrofos tipográficos
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


def _clean_text(raw: str) -> str:
    txt = htmllib.unescape(re.sub(r"<[^>]+>", " ", raw or ""))
    return re.sub(r"\s+", " ", txt.replace("\xa0", " ")).strip()


def analyze_description(desc: str, name: str):
    """Extrae motivos y mención de conflicto del texto del aviso. Devuelve None si no hay texto."""
    if not desc or not desc.strip():
        return None
    full = _clean_text(desc).replace("U.S.", "US")

    # La frase principal ("Do not travel ... due to <motivos>") puede venir partida en varios bloques HTML
    # (p. ej. "Do not travel" en un bloque y "to Uganda due to ..." en otro), por eso se busca en el texto unido.
    matches = list(HEAD_RE.finditer(full[:6000]))
    headline = None
    if matches:
        nn = norm_name(name)
        pick = next((m for m in matches if nn and nn in norm_name(m.group(0))), matches[0])
        rest = full[pick.end():pick.end() + 800]
        if pick.group(2) == ":" or not rest.strip():            # "due to:" seguido de una lista
            cut = END_LIST_RE.search(rest)
            tail = rest[:cut.start()] if cut else rest[:700]
        else:
            m_end = re.search(r"\.(?:\s|$)", rest)
            tail = rest[:m_end.start()] if m_end else rest[:500]
        tail = tail.strip()[:700]
        headline = re.sub(r"\s+", " ", full[pick.start():pick.end()] + tail).strip()
        motivos = [k for k, rx in MOTIVOS_RE if rx.search(tail)]
    else:
        motivos = []

    # Respaldo: en algunos avisos la lista de motivos está en otra oración ("... are at risk due to crime, health...").
    # Se acepta un "due to" de la parte alta del texto solo si su oración trae al menos 2 motivos del vocabulario.
    if headline is None:
        for m in re.finditer(r"\bdue to\b\s*:?\s*", full[:4000], re.I):
            rest = full[m.end():m.end() + 500]
            m_end = re.search(r"\.(?:\s|$)", rest)
            tail = (rest[:m_end.start()] if m_end else rest).strip()
            found = [k for k, rx in MOTIVOS_RE if rx.search(tail)]
            if len(found) >= 2:
                headline = re.sub(r"\s+", " ", full[max(0, m.start() - 80):m.end()] + tail).strip()
                motivos = found
                break

    # "Menciona": el aviso usa la expresión "armed conflict" en cualquier parte, o la frase principal
    # cita guerra/hostilidades/invasión. No afirma que haya guerra en todo el país.
    menciona = bool(re.search(r"armed conflict", full, re.I)) or "conflicto_armado" in motivos
    return {"motivos": motivos, "menciona_conflicto": menciona,
            "motivo_original": headline[:400] if headline else None, "_texto": full[:450]}


def fetch(url: str, retries: int = 3, host: str = ALLOWED_HOST) -> bytes:
    last = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                       "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.5"})
            with urllib.request.urlopen(req, timeout=30) as r:
                final_host = urllib.parse.urlparse(r.geturl()).hostname
                if final_host != host:
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
        name = re.sub(r"\s+travel advisory$", "", name, flags=re.I)
        if not name:
            anomalies.append(f"Sin nombre, omitido: {link}")
            continue
        if composite:
            skipped.append(f"Entrada compuesta, nombre tomado de la URL ({name}): {title!r}")

        iso = resolve_iso(name, iso_idx)
        if not iso:
            no_iso.append(name)
        cid = iso.lower() if iso else slug

        analysis = analyze_description(item.findtext("description") or "", name)
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
                "vigencia_dias": REVIEW_DAYS[nivel],
                "url": link,
            }],
        }
        if analysis is not None:
            SNIPPETS[cid] = analysis.pop("_texto", "")
            n0 = entry["niveles"][0]
            n0["motivos"] = analysis["motivos"]
            n0["menciona_conflicto"] = analysis["menciona_conflicto"]
            if analysis["motivo_original"]:
                n0["motivo_original"] = analysis["motivo_original"]
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


def audit_report(paises: dict):
    """Resumen legible para revisar la extracción de motivos (log y resumen del workflow)."""
    rows = sorted(paises.values(), key=lambda p: p["nombre"].casefold())
    conflicto, sin_motivos, tabla = [], [], []
    for p in rows:
        n = p["niveles"][0]
        if "motivos" not in n:
            continue
        if n["menciona_conflicto"]:
            conflicto.append(p["nombre"])
        if n["nivel"] >= 3:
            tabla.append((p["nombre"], n["nivel"], ", ".join(n["motivos"]) or "—", "sí" if n["menciona_conflicto"] else "no"))
            if not n["motivos"]:
                frase = n.get("motivo_original")
                if not frase:
                    frase = "sin frase principal | texto: " + SNIPPETS.get(p["id"], "")[:400]
                sin_motivos.append((p["nombre"], frase[:90] if n.get("motivo_original") else frase))
    return conflicto, sin_motivos, tabla


def retain_missing(prev: dict, paises: dict, today: str, grupos_presentes: set = None):
    """Conserva (hasta RETAIN_DAYS) los países de nivel 3-4, o con código ISO, que dejaron de aparecer en el feed.
    Un país de riesgo alto no debe desaparecer en silencio porque el feed falle o cambie de formato.
    `grupos_presentes` son los componentes (nombre normalizado) que la agrupación de territorios
    eliminó a propósito: no son un fallo del feed y no deben retenerse."""
    kept = []
    if not prev or prev.get("ejemplo", False):
        return kept
    names = {p["nombre"].casefold() for p in paises.values()}
    grupos_presentes = grupos_presentes or set()
    t = dt.date.fromisoformat(today)
    for old in prev.get("paises", []):
        oid = old.get("id") or old.get("iso")
        if not oid or oid in paises or old.get("nombre", "").casefold() in names:   # sigue en el feed o solo cambió de id
            continue
        if norm_name(old.get("nombre", "")) in grupos_presentes:   # lo absorbió una agrupación de territorios, no un fallo del feed
            continue
        niveles = old.get("niveles") or []
        if not (any((n.get("nivel") or 0) >= 3 for n in niveles) or old.get("iso")):
            continue
        first = next((n["retirado"] for n in niveles if n.get("retirado")), today)
        try:
            if (t - dt.date.fromisoformat(first)).days > RETAIN_DAYS:
                continue
        except ValueError:
            first = today
        copy = json.loads(json.dumps(old))
        for n in copy["niveles"]:
            n["retirado"] = first
        paises[oid] = copy
        kept.append((old.get("nombre", oid), first))
    return kept


def agrupar_territorios(paises: dict) -> list:
    """Si existe la entrada agrupada, elimina sus componentes de nivel 1-2.
    Devuelve líneas de log describiendo lo hecho."""
    acciones = []
    por_nombre = {norm_name(p["nombre"]): pid for pid, p in paises.items()}
    for grupo, componentes in GRUPOS_TERRITORIOS.items():
        gid = por_nombre.get(grupo)
        if not gid:
            continue
        for comp in componentes:
            cid = por_nombre.get(comp)
            if not cid or cid == gid:
                continue
            nivel = paises[cid]["niveles"][0].get("nivel")
            if isinstance(nivel, int) and 1 <= nivel <= 2:
                acciones.append(
                    f"agrupación: '{paises[gid]['nombre']}' resume a '{paises[cid]['nombre']}' "
                    f"(nivel {nivel}), componente eliminado")
                del paises[cid]
    return acciones


def componentes_de_grupos_presentes(paises: dict) -> set:
    """Nombres normalizados de los componentes de cada grupo de GRUPOS_TERRITORIOS cuya entrada
    agrupada está presente en `paises`. Sirve para que retain_missing no confunda un componente
    absorbido a propósito por la agrupación con un país que el feed dejó de traer."""
    por_nombre = {norm_name(p["nombre"]) for p in paises.values()}
    presentes = set()
    for grupo, componentes in GRUPOS_TERRITORIOS.items():
        if grupo in por_nombre:
            presentes.update(componentes)
    return presentes


def _prev_entry_map(prev: dict) -> dict:
    """(id, fuente) -> (pais, nivel_entry) del archivo anterior."""
    out = {}
    for p in prev.get("paises", []):
        key = p.get("id") or p.get("iso")
        for n in p.get("niveles", []):
            out[(key, n.get("fuente"))] = (p, n)
    return out


def restaurar_versiones_viejas(prev: dict, paises: dict) -> list:
    """REGLA 'no retroceder en el tiempo': si para el mismo (id, fuente) la fecha del aviso
    nuevo es ANTERIOR a la ya publicada, es una versión vieja del feed: se conserva la
    entrada previa. Devuelve líneas de log."""
    acciones = []
    prev_map = _prev_entry_map(prev)
    for pid, p in paises.items():
        for n in p.get("niveles", []):
            old = prev_map.get((pid, n.get("fuente")))
            if not old:
                continue
            f_new, f_old = n.get("fecha"), old[1].get("fecha")
            if f_new and f_old and f_new < f_old:
                acciones.append(
                    f"versión antigua ignorada: {p['nombre']} ({n.get('fuente')}): "
                    f"el feed trae {f_new}, ya publicado {f_old}; se conserva la entrada previa")
                n.clear()
                n.update(json.loads(json.dumps(old[1])))
    return acciones


def actualizar_historial_cambios(prev: dict, paises: dict, today: str):
    """Detecta cambios de nivel por (id, fuente) respecto al archivo anterior, marca las
    entradas y arrastra el historial raíz 'cambios' (máx. CAMBIOS_MAX_AGE días, CAMBIOS_KEEP
    registros, ordenado por fecha descendente). Devuelve (historial, cambios_nuevos)."""
    t = dt.date.fromisoformat(today)
    prev_map = _prev_entry_map(prev)
    cambios_nuevos = []
    for pid, p in paises.items():
        for n in p.get("niveles", []):
            old_pair = prev_map.get((pid, n.get("fuente")))
            if old_pair:
                _, n_old = old_pair
                nivel_old, nivel_new = n_old.get("nivel"), n.get("nivel")
                # No cuentan: entradas nuevas (sin par previo), retiradas ni renombradas
                if (not n.get("retirado") and not n_old.get("retirado")
                        and isinstance(nivel_old, int) and isinstance(nivel_new, int)
                        and nivel_old != nivel_new):
                    n["nivel_anterior"] = nivel_old
                    n["cambio"] = today
                    cambios_nuevos.append({
                        "id": pid, "iso": p.get("iso"), "nombre": p.get("nombre"),
                        "fuente": n.get("fuente"), "de": nivel_old, "a": nivel_new, "fecha": today,
                    })
                    continue
                # Arrastrar la marca anterior mientras tenga menos de CAMBIOS_MAX_AGE días
                marca, anterior = n_old.get("cambio"), n_old.get("nivel_anterior")
                if marca and isinstance(anterior, int):
                    try:
                        if (t - dt.date.fromisoformat(marca)).days <= CAMBIOS_MAX_AGE:
                            n["nivel_anterior"] = anterior
                            n["cambio"] = marca
                    except ValueError:
                        pass
    historial = []
    prev_cambios = prev.get("cambios")
    if isinstance(prev_cambios, list):
        for c in prev_cambios:
            try:
                if (t - dt.date.fromisoformat(c.get("fecha", ""))).days > CAMBIOS_MAX_AGE:
                    continue
            except (ValueError, AttributeError):
                continue
            historial.append(c)
    historial.extend(cambios_nuevos)
    historial.sort(key=lambda c: c.get("fecha", ""), reverse=True)
    return historial[:CAMBIOS_KEEP], cambios_nuevos


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
    ap.add_argument("--input", nargs="+", help="Leer el feed de uno o más archivos locales (pruebas) en vez de descargarlo")
    ap.add_argument("--output", default=str(root / "data" / "levels.json"))
    ap.add_argument("--min-items", type=int, default=DEFAULT_MIN_ITEMS)
    args = ap.parse_args()
    out_path = Path(args.output)

    if args.input:
        xml_list = [Path(f).read_bytes() for f in args.input]
    else:
        xml_list, last_err = [], None
        for i in range(FETCH_ROUNDS):
            try:
                xml_list.append(fetch(FEED_URL, retries=3 if i == 0 else 1))
            except Exception as e:                  # noqa: BLE001
                last_err = e
            if i < FETCH_ROUNDS - 1:
                time.sleep(FETCH_PAUSE_S)
        if not xml_list:
            print(f"ERROR de descarga: {last_err}", file=sys.stderr)
            return 2

    iso_idx = build_iso_index()
    paises, anomalies, skipped, no_iso, counts = {}, [], [], [], []
    for xml_bytes in xml_list:
        try:
            p_i, a_i, s_i, n_i = parse_feed(xml_bytes, iso_idx)
        except Exception as e:                      # noqa: BLE001
            print(f"AVISO: una descarga del feed no se pudo leer y se ignora: {e}", file=sys.stderr)
            continue
        counts.append(len(p_i))
        anomalies += [x for x in a_i if x not in anomalies]
        skipped += [x for x in s_i if x not in skipped]
        no_iso += n_i
        for cid, entry in p_i.items():              # unión: se conserva la versión con fecha más reciente
            if cid not in paises or entry["niveles"][0]["fecha"] > paises[cid]["niveles"][0]["fecha"]:
                paises[cid] = entry
    if not counts:
        print("ERROR: feed inválido en todas las descargas, no se escribió nada.", file=sys.stderr)
        return 1
    print(f"Descargas del feed: {len(counts)} · avisos por descarga: {counts} · unión: {len(paises)}")

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

    # No retroceder en el tiempo: el feed puede reintroducir una versión vieja de un aviso
    for line in restaurar_versiones_viejas(prev, paises):
        print(line, file=sys.stderr)
    # Territorios agrupados: la entrada conjunta resume a sus componentes de nivel 1-2
    for line in agrupar_territorios(paises):
        print(line)

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
    faltan = [nombre for iso, nombre in CANARIOS.items() if iso.lower() not in paises]
    if faltan:
        print(f"ALERTA: el feed NO trae países que sí tienen aviso: {', '.join(faltan)}", file=sys.stderr)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
                f.write(f"\n## ALERTA: faltan en el feed\n\n{', '.join(faltan)}\n")
    grupos_presentes = componentes_de_grupos_presentes(paises)
    retained = retain_missing(prev, paises, today, grupos_presentes)
    for nombre, desde in retained:
        print(f"RETENIDO (ya no figura en el feed desde {desde}; se conserva hasta {RETAIN_DAYS} días): {nombre}", file=sys.stderr)
    cambios, cambios_nuevos = actualizar_historial_cambios(prev, paises, today)
    for c in cambios_nuevos:
        print(f"CAMBIO DE NIVEL: {c['nombre']}: {c['de']} → {c['a']} ({c['fuente']}, {today})")
    historial_desde = prev.get("historial_desde") or today

    new = {
        "generado": today,
        "ejemplo": False,
        "fuentes": [{"id": SOURCE_ID, "nombre": SOURCE_NAME, "url": SOURCE_URL,
                     "nota": "Contenido oficial publicado por el Departamento de Estado de EE.UU. (travel.state.gov)."}],
        "historial_desde": historial_desde,
        "cambios": cambios,
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

    conflicto, sin_motivos, tabla = audit_report(paises)
    print(f"Avisos que mencionan conflicto armado o guerra ({len(conflicto)}): {', '.join(conflicto)}")
    print(f"Nivel 3-4 SIN motivos reconocidos ({len(sin_motivos)}):")
    for nombre, frase in sin_motivos:
        print(f"  - {nombre}: {frase}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        lines = ["## Revisión de motivos (niveles 3 y 4)", "",
                 f"Países: {n} · mencionan conflicto: {len(conflicto)} · nivel 3-4 sin motivos reconocidos: {len(sin_motivos)}", "",
                 "| País | Nivel | Motivos detectados | ¿Menciona conflicto? |", "|---|---|---|---|"]
        lines += [f"| {a} | {b} | {c} | {d} |" for a, b, c, d in tabla]
        with open(summary, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")

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
