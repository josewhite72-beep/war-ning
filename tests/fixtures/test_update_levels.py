#!/usr/bin/env python3
"""Pruebas de scripts/update_levels.py (solo biblioteca estándar, sin red).

Requiere: fixture_feed.xml copiado en tests/fixtures/fixture_feed.xml
Ejecutar desde la raíz del repo:  python -m unittest discover -s tests
"""
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "fixture_feed.xml"

spec = importlib.util.spec_from_file_location("update_levels", ROOT / "scripts" / "update_levels.py")
ul = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ul)

FUENTE = ul.SOURCE_NAME
TODAY = "2026-09-20"


def feed_xml(items):
    """Mini-feed sintético. items: lista de (title, link, pubDate, description, threat_category)."""
    parts = ['<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel>',
             '<title>travel.state.gov: Travel Advisories</title>']
    for title, link, date, desc, cat in items:
        parts.append(f"<item><title>{escape(title)}</title><link>{escape(link)}</link>"
                     f"<pubDate>{escape(date)}</pubDate>"
                     f"<description><![CDATA[{desc}]]></description>"
                     f'<category domain="Threat-Level">{escape(cat)}</category></item>')
    parts.append("</channel></rss>")
    return "".join(parts).encode("utf-8")


def item(name, nivel, fecha="Mon, 01 Jun 2026", desc="", slug=None):
    slug = slug or name.lower().replace(" ", "-")
    return (f"{name} - Level {nivel}: Exercise Normal Precautions",
            f"https://travel.state.gov/content/travel/en/traveladvisories/traveladvisories/{slug}-travel-advisory.html",
            fecha, desc or f"<p>Exercise normal precautions in {name}.</p>",
            f"Level {nivel}: Exercise Normal Precautions")


def pais(pid, nombre, nivel, fecha="2026-08-01", iso=None, fuente=FUENTE, extra=None):
    n = {"fuente": fuente, "escala": "1-4", "min": 1, "max": 4, "nivel": nivel,
         "etiqueta": "x", "fecha": fecha, "url": "https://travel.state.gov/"}
    if extra:
        n.update(extra)
    return {"id": pid, "iso": iso, "nombre": nombre, "niveles": [n]}


def union(feeds):
    """Replica la unión de descargas de main() (conserva la fecha más reciente por id)."""
    iso_idx = ul.build_iso_index()
    paises = {}
    for xml in feeds:
        p, a, s, n = ul.parse_feed(xml, iso_idx)
        for cid, e in p.items():
            if cid not in paises or e["niveles"][0]["fecha"] > paises[cid]["niveles"][0]["fecha"]:
                paises[cid] = e
    return paises


class TestFixtureMotivos(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paises, cls.anomalies, cls.skipped, cls.no_iso = ul.parse_feed(
            FIXTURE.read_bytes(), ul.build_iso_index())

    def motivos(self, nombre):
        p = next(p for p in self.paises.values() if p["nombre"] == nombre)
        n = p["niveles"][0]
        return set(n.get("motivos", [])), n.get("menciona_conflicto", False)

    def test_motivos_esperados(self):
        casos = {
            "Iraq": ({"conflicto_armado", "terrorismo", "crimen", "secuestro", "salud"}, True),
            "United Arab Emirates": ({"conflicto_armado", "terrorismo"}, True),
            "Qatar": ({"conflicto_armado"}, True),
            "Ukraine": ({"conflicto_armado"}, True),
            "Kuwait": ({"conflicto_armado", "crimen", "minas"}, True),
            "Ethiopia": ({"terrorismo", "crimen", "disturbios", "secuestro", "minas"}, True),
            "Israel": ({"terrorismo", "disturbios"}, True),
            "West Bank": ({"terrorismo", "disturbios"}, False),
            "Gaza": ({"terrorismo", "disturbios", "salud"}, True),
            "Bosnia and Herzegovina": ({"terrorismo", "crimen", "minas"}, False),
            "Serbia": ({"crimen"}, False),
            "Albania": (set(), False),
            "Suriname": (set(), False),
            "Montenegro": (set(), False),
            "Macau": (set(), False),
            "Yland": (set(), False),
            "Laos": ({"minas"}, False),  # la guerra de Vietnam solo aparece en el texto sobre minas
            "Xland": ({"salud", "desastres_naturales"}, False),
            "Uganda": ({"crimen", "salud", "terrorismo", "disturbios"}, False),   # frase partida en bloques
            "Uganda2": ({"crimen", "salud", "terrorismo", "disturbios"}, False),  # respaldo "at risk due to"
            "Belarus": ({"conflicto_armado"}, True),
            "Russia": ({"conflicto_armado", "terrorismo", "detencion_injusta"}, True),
            "Mali": ({"crimen", "terrorismo", "secuestro", "disturbios", "salud"}, False),
            "Mexico": ({"crimen", "secuestro"}, False),
        }
        for nombre, (motivos, menciona) in casos.items():
            with self.subTest(pais=nombre):
                m, mc = self.motivos(nombre)
                self.assertEqual(m, motivos)
                self.assertEqual(mc, menciona)

    def test_israel_titulo_con_nivel_duplicado(self):
        p = next(p for p in self.paises.values() if p["nombre"] == "Israel")
        self.assertEqual(p["niveles"][0]["nivel"], 3)

    def test_todos_los_items_validos(self):
        # el fixture no debe producir anomalías ni omitidos
        self.assertEqual(self.anomalies, [])
        self.assertEqual(self.skipped, [])


class TestAnomaliasSinteticas(unittest.TestCase):
    def setUp(self):
        self.iso_idx = ul.build_iso_index()

    def parse(self, items):
        return ul.parse_feed(feed_xml(items), self.iso_idx)

    def test_entrada_compuesta_china(self):
        it = ("China, Hong Kong, and Macau - Level 3: Reconsider Travel - See Individual Summaries",
              "https://travel.state.gov/content/travel/en/traveladvisories/traveladvisories/china-travel-advisory.html",
              "Mon, 01 Jun 2026", "<p>Reconsider travel to mainland China due to arbitrary enforcement of local laws.</p>",
              "Level 3: Reconsider Travel")
        paises, anomalies, skipped, no_iso = self.parse([it])
        self.assertEqual(anomalies, [])
        self.assertIn("cn", paises)
        self.assertEqual(paises["cn"]["nombre"], "China")       # nombre tomado de la URL
        self.assertEqual(paises["cn"]["niveles"][0]["nivel"], 3)
        self.assertTrue(any("Entrada compuesta" in s for s in skipped))

    def test_nivel_contradictorio(self):
        it = ("Serbia - Level 3: Reconsider Travel",
              "https://travel.state.gov/content/travel/en/traveladvisories/traveladvisories/serbia-travel-advisory.html",
              "Mon, 01 Jun 2026", "<p>Texto.</p>", "Level 1: Exercise Normal Precautions")
        paises, anomalies, skipped, no_iso = self.parse([it])
        self.assertEqual(paises, {})
        self.assertTrue(any("contradictorio" in a for a in anomalies))

    def test_fecha_invalida(self):
        it = ("Serbia - Level 2: Exercise Increased Caution",
              "https://travel.state.gov/content/travel/en/traveladvisories/traveladvisories/serbia-travel-advisory.html",
              "no es una fecha", "<p>Texto.</p>", "Level 2: Exercise Increased Caution")
        paises, anomalies, skipped, no_iso = self.parse([it])
        self.assertEqual(paises, {})
        self.assertTrue(any("Fecha inválida" in a for a in anomalies))

    def test_enlace_fuera_de_dominio(self):
        it = ("Serbia - Level 2: Exercise Increased Caution",
              "https://otro-sitio.example.com/serbia-travel-advisory.html",
              "Mon, 01 Jun 2026", "<p>Texto.</p>", "Level 2: Exercise Increased Caution")
        paises, anomalies, skipped, no_iso = self.parse([it])
        self.assertEqual(paises, {})
        self.assertTrue(any("fuera de travel.state.gov" in a for a in anomalies))

    def test_worldwide_caution_sin_nivel(self):
        it = ("Worldwide Caution: Avoid travel",
              "https://travel.state.gov/content/travel/en/traveladvisories/traveladvisories/worldwide-caution.html",
              "Mon, 01 Jun 2026", "<p>Texto.</p>", "Caution")
        paises, anomalies, skipped, no_iso = self.parse([it])
        self.assertEqual(paises, {})
        self.assertTrue(any("Sin nivel 1-4" in s for s in skipped))


class TestUnionYRetencion(unittest.TestCase):
    def test_union_varias_descargas(self):
        a = feed_xml([item("Serbia", 2, "Thu, 27 Aug 2026"), item("Iraq", 4, "Sat, 29 Aug 2026")])
        b = feed_xml([item("Serbia", 3, "Tue, 01 Sep 2026"), item("Qatar", 3, "Fri, 28 Aug 2026")])
        paises = union([a, b])
        self.assertEqual(len(paises), 3)                       # unión sin duplicados
        self.assertEqual(paises["rs"]["niveles"][0]["nivel"], 3)  # fecha más reciente gana
        self.assertEqual(paises["rs"]["niveles"][0]["fecha"], "2026-09-01")
        self.assertIn("iq", paises)                            # país solo en la descarga A
        self.assertIn("qa", paises)                            # país solo en la descarga B

    def test_retencion_paises_desaparecidos(self):
        prev = {"ejemplo": False, "paises": [
            pais("sy", "Syria", 4, "2026-06-01", iso="SY"),
            pais("xx", "Tierrabaja", 1, "2026-06-01", iso=None),   # nivel 1-2 sin ISO: no se retiene
        ]}
        paises = union([feed_xml([item("Serbia", 2)])])
        kept = ul.retain_missing(prev, paises, TODAY)
        self.assertIn("sy", paises)
        self.assertEqual(paises["sy"]["niveles"][0]["retirado"], TODAY)
        self.assertNotIn("xx", paises)
        self.assertEqual([k[0] for k in kept], ["Syria"])

    def test_retencion_expira_a_30_dias(self):
        prev = {"ejemplo": False, "paises": [
            pais("sy", "Syria", 4, "2026-06-01", iso="SY", extra={"retirado": "2026-06-01"}),
        ]}
        paises = {}
        kept = ul.retain_missing(prev, paises, "2026-07-15")   # 44 días después: expira
        self.assertEqual(kept, [])
        self.assertEqual(paises, {})


class TestAgrupacionTerritorios(unittest.TestCase):
    def test_agrupacion_b(self):
        items = [
            item("Saba and Sint Eustatius", 2, slug="saba-and-sint-eustatius"),
            item("Saba", 1),
            item("Sint Eustatius", 1),
            item("French West Indies", 2, slug="french-west-indies"),
            item("Guadeloupe", 2),
            item("Saint Barthelemy", 2),
            item("Martinique", 3),   # nivel 3: NO se elimina aunque exista la agrupada
        ]
        paises = union([feed_xml(items)])
        log = ul.agrupar_territorios(paises)     # en main() se aplica tras la unión
        self.assertEqual(len(log), 4)            # Saba, Sint Eustatius, Guadeloupe, Saint Barthelemy
        nombres = {p["nombre"] for p in paises.values()}
        self.assertIn("Saba and Sint Eustatius", nombres)
        self.assertIn("French West Indies", nombres)
        self.assertIn("Martinique", nombres)
        self.assertNotIn("Saba", nombres)
        self.assertNotIn("Sint Eustatius", nombres)
        self.assertNotIn("Guadeloupe", nombres)
        self.assertNotIn("Saint Barthelemy", nombres)

    def test_componente_nivel_3_se_conserva(self):
        # sin la entrada agrupada no se elimina nada
        paises = union([feed_xml([item("Saba", 1), item("Sint Eustatius", 2)])])
        self.assertEqual(ul.agrupar_territorios(paises), [])
        self.assertEqual(len(paises), 2)


class TestCambiosYRetroceso(unittest.TestCase):
    def test_deteccion_de_cambios(self):
        prev = {"paises": [pais("iq", "Iraq", 3, "2026-08-01", iso="IQ"),
                           pais("ir", "Iran", 4, "2026-08-01", iso="IR")]}
        paises = {"iq": pais("iq", "Iraq", 4, TODAY, iso="IQ"),
                  "ir": pais("ir", "Iran", 4, TODAY, iso="IR")}   # sin cambio
        historial, nuevos = ul.actualizar_historial_cambios(prev, paises, TODAY)
        self.assertEqual(len(nuevos), 1)
        c = nuevos[0]
        self.assertEqual((c["id"], c["de"], c["a"], c["fecha"]), ("iq", 3, 4, TODAY))
        self.assertEqual(c["nombre"], "Iraq")
        self.assertEqual(c["fuente"], FUENTE)
        n = paises["iq"]["niveles"][0]
        self.assertEqual(n["nivel_anterior"], 3)
        self.assertEqual(n["cambio"], TODAY)
        self.assertNotIn("cambio", paises["ir"]["niveles"][0])     # sin cambio: sin marca
        self.assertEqual(historial[0]["id"], "iq")                 # ordenado desc por fecha

    def test_no_cuentan_nuevas_ni_retiradas(self):
        prev = {"paises": [pais("sy", "Syria", 4, "2026-06-01", iso="SY",
                                extra={"retirado": "2026-09-01"})]}
        paises = {"sy": pais("sy", "Syria", 3, TODAY, iso="SY"),   # reaparece: era retirado
                  "ve": pais("ve", "Venezuela", 3, TODAY, iso="VE")}  # nueva: sin par previo
        _, nuevos = ul.actualizar_historial_cambios(prev, paises, TODAY)
        self.assertEqual(nuevos, [])

    def test_arrastre_marca_menos_de_60_dias(self):
        prev = {"paises": [pais("iq", "Iraq", 4, "2026-09-01", iso="IQ",
                                extra={"nivel_anterior": 3, "cambio": "2026-09-10"})]}
        paises = {"iq": pais("iq", "Iraq", 4, TODAY, iso="IQ")}
        _, nuevos = ul.actualizar_historial_cambios(prev, paises, TODAY)
        self.assertEqual(nuevos, [])                                # nivel sin cambio
        n = paises["iq"]["niveles"][0]
        self.assertEqual(n["nivel_anterior"], 3)                    # marca arrastrada
        self.assertEqual(n["cambio"], "2026-09-10")

    def test_arrastre_expira_a_60_dias(self):
        prev = {"paises": [pais("iq", "Iraq", 4, "2026-09-01", iso="IQ",
                                extra={"nivel_anterior": 3, "cambio": "2026-06-01"})]}
        paises = {"iq": pais("iq", "Iraq", 4, TODAY, iso="IQ")}
        ul.actualizar_historial_cambios(prev, paises, TODAY)
        self.assertNotIn("cambio", paises["iq"]["niveles"][0])

    def test_historial_arrastrado_limitado_y_limpiado(self):
        viejo = {"id": "zz", "iso": "ZZ", "nombre": "Zululandia", "fuente": FUENTE,
                 "de": 1, "a": 2, "fecha": "2026-06-01"}            # >60 días: se elimina
        prev = {"paises": [], "cambios": [viejo]}
        historial, _ = ul.actualizar_historial_cambios(prev, {}, TODAY)
        self.assertEqual(historial, [])

    def test_regla_no_retroceder(self):
        prev = {"paises": [pais("iq", "Iraq", 3, "2026-09-10", iso="IQ")]}
        paises = {"iq": pais("iq", "Iraq", 4, "2026-08-01", iso="IQ")}   # fecha anterior: versión vieja
        log = ul.restaurar_versiones_viejas(prev, paises)
        self.assertEqual(len(log), 1)
        self.assertIn("versión antigua ignorada", log[0])
        n = paises["iq"]["niveles"][0]
        self.assertEqual((n["nivel"], n["fecha"]), (3, "2026-09-10"))   # se conserva la previa

    def test_fecha_posterior_no_se_toca(self):
        prev = {"paises": [pais("iq", "Iraq", 3, "2026-08-01", iso="IQ")]}
        paises = {"iq": pais("iq", "Iraq", 4, "2026-09-01", iso="IQ")}
        self.assertEqual(ul.restaurar_versiones_viejas(prev, paises), [])
        self.assertEqual(paises["iq"]["niveles"][0]["nivel"], 4)


class TestMainFlujo(unittest.TestCase):
    def setUp(self):
        # Sin esto, en Actions las pruebas añadirían ALERTA y tablas del fixture al resumen real de la ejecución
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("GITHUB_STEP_SUMMARY", None)
        os.environ.pop("COMMIT_MSG_FILE", None)

    def run_main(self, extra, out_path):
        argv = ["update_levels.py", "--input", str(FIXTURE), "--output", str(out_path)] + extra
        with mock.patch.object(sys, "argv", argv):
            return ul.main()

    def test_aborta_con_menos_de_150_avisos(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "levels.json"          # no existe: sin archivo previo
            self.assertEqual(self.run_main([], out), 1)
            self.assertFalse(out.exists())          # no se escribió nada

    def test_aborta_con_caida_mayor_al_15(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "levels.json"
            prev = {"ejemplo": False,
                    "paises": [pais(f"p{i}", f"Pais{i}", 2) for i in range(200)]}
            out.write_text(json.dumps(prev), encoding="utf-8")
            self.assertEqual(self.run_main(["--min-items", "5"], out), 1)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(data["paises"]), 200)   # archivo intacto

    def test_escritura_ok_con_min_items_bajo(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "levels.json"
            self.assertEqual(self.run_main(["--min-items", "5"], out), 0)
            data = json.loads(out.read_text(encoding="utf-8"))
            self.assertIn("cambios", data)               # campos nuevos presentes
            self.assertIn("historial_desde", data)
            self.assertEqual(data["cambios"], [])
            self.assertGreaterEqual(len(data["paises"]), 25)

    def test_flujo_completo_cambio_y_no_retroceso(self):
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            out = td / "levels.json"
            vieja = td / "vieja.xml"
            nueva = td / "nueva.xml"
            vieja.write_bytes(feed_xml([item("Serbia", 2, "Thu, 27 Aug 2026")]))
            nueva.write_bytes(feed_xml([item("Serbia", 3, "Fri, 18 Sep 2026")]))

            def correr(feed):
                argv = ["update_levels.py", "--input", str(feed), "--output", str(out), "--min-items", "1"]
                with mock.patch.object(sys, "argv", argv):
                    self.assertEqual(ul.main(), 0)
                return json.loads(out.read_text(encoding="utf-8"))

            d1 = correr(vieja)
            self.assertEqual(d1["cambios"], [])
            d2 = correr(nueva)                                   # sube de 2 a 3
            self.assertEqual([(c["nombre"], c["de"], c["a"]) for c in d2["cambios"]], [("Serbia", 2, 3)])
            n = d2["paises"][0]["niveles"][0]
            self.assertEqual((n["nivel"], n["nivel_anterior"]), (3, 2))
            d3 = correr(vieja)                                   # el feed devuelve la versión vieja
            n = d3["paises"][0]["niveles"][0]
            self.assertEqual((n["nivel"], n["fecha"]), (3, "2026-09-18"))
            self.assertEqual(len(d3["cambios"]), 1)              # sin cambios nuevos


if __name__ == "__main__":
    unittest.main()
