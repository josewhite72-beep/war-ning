# Estado del proyecto WAR-NING

Última actualización: 2026-09-21. Este documento existe para retomar el proyecto sin depender
de ninguna conversación previa con Claude Code.

## 1. Qué es hoy la app

Un visor de los avisos de viaje del Departamento de Estado de EE. UU., país por país. No predice
conflictos ni emite alertas propias: es un agregador que muestra cada aviso con su escala, su fecha
y un enlace al original (`index.html`, sección «Acerca de»).

Pestañas (nav inferior de `index.html`):
- **Panorama**: lista de países con buscador y filtros por nivel/motivo.
- **Mis regiones**: países marcados como favoritos por el usuario (guardado local).
- **Cambios**: historial de cambios de nivel detectados en los datos (se muestran los de los
  últimos 30 días; se conservan hasta 60 días o 300 registros).
- **Alertas**: placeholder «Próximamente» para una Fase 2 (alertas inminentes por región); hoy no
  tiene funcionalidad, solo describe un esquema futuro.
- **Ajustes**: apariencia (Modo Supervivencia), borrar caché/recargar, y «Acerca de» con la versión
  y las notas legales del agregador.

Versión visible actual en «Acerca de»: **v1.6**.

## 2. Estado actual

El proyecto está **en pausa desde 2026-09-21**. El flujo de GitHub Actions que actualiza
`data/levels.json` cada 6 horas (`update-levels.yml`) está **desactivado a mano** (verificado por
API de GitHub: estado `disabled_manually`). Los datos quedan **congelados** en su último valor
hasta que se reactive.

**No compartir el enlace de la app** hasta reactivar el flujo con las protecciones reforzadas (ver
sección 4 y pendientes).

Despliegue: no hay `vercel.json` ni configuración de Vercel en el repositorio, así que no se puede
confirmar desde el código si el despliegue a producción es automático en cada push a `main`.
**No verificado.**

## 3. Incidente del 2026-09-21

El bot publicó, sin motivo aparente, **Irak en nivel 1** (real: 4) y **Bélgica en nivel 1**
(real: 2), ambos con fecha del propio día y sin `motivos` ni `motivo_original`, mientras el resto
del feed de esa fecha sí traía esos campos con normalidad.

Cómo se detectó: la línea de log `CAMBIO DE NIVEL` (impresa por `scripts/update_levels.py`) y la
pestaña «Cambios» de la app.

Corrección: PR #3 («Datos: corrige aviso erróneo de Irak y Bélgica (nivel 1 falso)»), que restaura
cada país a su última versión buena del historial: Irak a nivel 4 (fecha 2026-08-29, motivo
original íntegro) y Bélgica a nivel 2 (fecha 2026-07-23, motivo original íntegro). **Fusionado en
`main`** mediante el commit de merge `12d9d3d0de7b994a551870f95a9bc0e876c8314c` (fusiona
`fix/restaura-nivel-iraq-belgica`). Verificado tras la fusión: Irak nivel 4, Bélgica nivel 2, campo
`cambios` vacío, 214 países en total (mismo recuento que antes).

Causa probable (no confirmada con certeza absoluta): el feed oficial mezcla avisos con dos formatos
de enlace, y el script se queda con la versión de fecha más reciente para cada país. Importante:
el formato de enlace `/tsg_aem/` **no es por sí solo señal de error** — lo usan 72 países del feed
con datos correctos; el problema apareció en la combinación de ese formato con la ausencia de
motivos y una fecha "ganadora" que no traía el contenido esperado.

## 4. Decisiones y lecciones

- Ningún cambio de nivel debe publicarse solo: siempre hay que verificarlo contra
  `travel.state.gov` antes de darlo por bueno.
- El script, tal como está, **conserva la versión con la fecha más nueva aunque sea errónea**
  (regla "no retroceder en el tiempo" en `restaurar_versiones_viejas`). Esto fue lo que dejó pasar
  el nivel 1 falso de Irak y Bélgica: la fecha nueva "ganó" aunque el contenido era peor.
- El feed oficial es inestable: omite países y a veces devuelve versiones viejas. Las protecciones
  existentes (4 descargas unidas, no retroceder en fechas, retención de 30 días para países que
  desaparecen, aviso si faltan países críticos) no deben debilitarse.

## 5. Pendientes (en orden)

1. Que los cambios de nivel se publiquen por pull request con aprobación humana, no directo a
   `main` desde el flujo automático.
2. Comprobar que `retain_missing` omite los componentes de una agrupación de territorios
   (`GRUPOS_TERRITORIOS`) solo cuando esos componentes son de nivel 1 o 2 (coherente con
   `agrupar_territorios`, que solo elimina componentes de nivel 1-2).
3. Más cobertura de pruebas en `tests/test_update_levels.py`:
   - `audit_report` (casos con y sin motivos, con y sin mención de conflicto).
   - `resolve_iso` con nombres que incluyen paréntesis.
   - `prev.json` corrupto (JSON inválido) al leer el historial previo.
   - XML corrupto en una de las 4 descargas del feed.
   - `parse_date` con desfases numéricos / formatos de fecha atípicos.
4. Reactivar el flujo de GitHub Actions (`update-levels.yml` → pestaña Actions → Enable workflow)
   cuando las protecciones anteriores estén reforzadas.
5. No existe `README.md` en el repositorio; escribir uno.
6. Accesibilidad de `index.html` (roles ARIA de pestañas, contraste, navegación por teclado).
7. Cabeceras de Vercel (seguridad/caché) — pendiente de revisar junto con el punto de despliegue
   no verificado de la sección 2.

## 6. Cómo retomar

1. Leer este archivo completo y `CLAUDE.md`.
2. `git fetch origin main && git log origin/main -10 --oneline` para ver si hubo actividad desde
   la fecha de "última actualización" de arriba.
3. Confirmar que el flujo `update-levels.yml` sigue desactivado (pestaña Actions del repo en
   GitHub) antes de tocar nada relacionado con datos en vivo.
4. `pip install "defusedxml==0.7.1" "pycountry==26.2.16" && python -m unittest discover -s tests`
   para confirmar que la base sigue verde (26 pruebas en la última verificación).
5. Revisar la pestaña «Cambios» de la app y `data/levels.json` para confirmar que Irak y Bélgica
   siguen en sus niveles reales (4 y 2) antes de dar cualquier cosa por buena.
6. Elegir un pendiente de la sección 5, empezando por el primero sin resolver, y trabajarlo en una
   rama propia con una sola tarea, siguiendo `CLAUDE.md`.
7. Solo cuando el punto 1 de pendientes (revisión humana de cambios de nivel) y el punto 4
   (reactivar el flujo) estén resueltos y probados, considerar reactivar las actualizaciones
   automáticas y, después, compartir el enlace de la app.
