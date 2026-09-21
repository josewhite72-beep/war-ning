# WAR-NING — instrucciones para Claude Code

Estado actual y pendientes: ver ESTADO.md

## Qué es
PWA estática (HTML + JavaScript sin frameworks) que muestra niveles de riesgo de viaje por país.
Fuente actual: avisos de viaje del Departamento de Estado de EE.UU. Un flujo de GitHub Actions
actualiza los datos cada 6 horas. La interfaz y los mensajes de commit van en español.

## Estructura
- `index.html`: toda la app (HTML, CSS y JS en un solo archivo; sin módulos ES, sin frameworks, sin paso de compilación).
- `sw.js`, `manifest.json`, iconos: PWA y modo sin conexión.
- `data/levels.json`: lo escribe el bot. **No editar a mano.**
- `scripts/update_levels.py`: descarga y normaliza el feed (Python 3.12; solo `defusedxml` y `pycountry`).
- `tests/`: pruebas con `unittest`, sin red. Feed de prueba en `tests/fixtures/fixture_feed.xml`.
- `.github/workflows/update-levels.yml`: ejecuta las pruebas y luego actualiza los datos.

## Cómo comprobar tu trabajo
```
pip install "defusedxml==0.7.1" "pycountry==26.2.16"
python -m unittest discover -s tests
```
Deben pasar todas las pruebas antes de proponer cambios. Si cambias el script, añade o ajusta pruebas.

## Principios que no se negocian
- Es un agregador: no inventa niveles ni los combina en una cifra propia. Cada fuente se muestra con su escala, su fecha y un enlace al original.
- Sin geolocalización, cuentas de usuario, analíticas ni librerías externas en el navegador.
- Todo texto que venga de los datos se escapa con `esc()` y los enlaces pasan por `safeUrl()`.
- Los campos nuevos del JSON son siempre opcionales: la app debe funcionar con un `levels.json` que no los tenga.
- El feed oficial es inestable (omite países y devuelve versiones viejas). El script ya lo descarga 4 veces y une los resultados, no retrocede a fechas anteriores, conserva 30 días los países que desaparecen y avisa si faltan países críticos. No debilitar esas protecciones.
- No afirmar más de lo que dicen los datos: «menciona conflicto armado» no significa que haya guerra.

## Reglas de trabajo
- Cambios pequeños: una tarea por rama.
- No modificar `.github/workflows/` salvo que se pida expresamente.
- Cuando cambie la app, subir la versión visible en «Acerca de» de `index.html`.
- Al terminar, resumir qué cambió y qué se probó.
- Si algo no es viable o es ambiguo, decirlo antes de inventar una solución.
