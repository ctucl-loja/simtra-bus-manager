"""
Recarga manual del itinerario: validación y fusión.

Funciones PURAS, sin red, sin base de datos y sin FastAPI. El endpoint
(`POST /api/dispatch/refresh` en main.py) las compone; aquí solo viven las dos
decisiones delicadas:

  1. ¿El despacho que acaba de bajar del backend remoto es utilizable?
     Una lista con contenido inválido NO es un día sin despachos. Si se
     confundieran, un error del backend vaciaría el itinerario del conductor a
     mitad de jornada y la pantalla diría "sin despacho para hoy" como si fuera
     un dato bueno.

  2. ¿Qué marcaciones locales hay que conservar al reemplazarlo?
     El bus pudo cruzar geocercas mientras la descarga estaba en curso, y esas
     marcaciones todavía no están en el backend remoto: si se reemplaza el
     despacho a secas, desaparecen de la pantalla.

La fusión usa IDENTIDADES ESTABLES —(número de step, id de checkpoint)— nunca
posiciones del array: el backend puede devolver los steps en otro orden o con
uno menos, y una fusión por índice trasladaría la llegada de un recorrido a
otro. Una marcación que no encuentra su sitio exacto NO se traslada: se
descarta y se deja dicho.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("simtra")

# Valor con el que el backend representa "todavía no llegó".
NOT_REPORTED = "00:00:00"


# ─────────────────────────────────────────────
# VALIDACIÓN
# ─────────────────────────────────────────────

@dataclass(frozen=True)
class Validation:
    valid: bool
    reason: Optional[str] = None   # texto en español, apto para la pantalla

    def __bool__(self) -> bool:
        return self.valid


def _is_schedule(value) -> bool:
    """'HH:MM:SS' con rango real. Un horario imposible no define una ventana."""
    if not isinstance(value, str):
        return False
    parts = value.strip().split(":")
    if len(parts) != 3:
        return False
    try:
        hours, minutes, seconds = (int(part) for part in parts)
    except ValueError:
        return False
    if not all(part.isdigit() for part in parts):
        return False
    return 0 <= hours < 24 and 0 <= minutes < 60 and 0 <= seconds < 60


def _is_id(value) -> bool:
    """Entero utilizable como identificador. `True` no es un id."""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_coordinate(value, limit: float) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return value == value and abs(value) <= limit   # value == value descarta NaN


def validate_checkpoint(raw) -> Optional[str]:
    """None si es utilizable; si no, el motivo."""
    if not isinstance(raw, dict):
        return "un punto de control no es un objeto"
    if not _is_id(raw.get("id")):
        return "un punto de control no tiene identificador"

    point = raw.get("point")
    if not isinstance(point, dict):
        return "un punto de control no trae su punto asociado"
    if not _is_id(point.get("id")):
        return "un punto no tiene identificador"
    if not _is_coordinate(point.get("latitude"), 90) or not _is_coordinate(point.get("longitude"), 180):
        return "un punto no tiene coordenadas utilizables"

    # time_calculated es el horario que la pantalla muestra como "punto actual";
    # sin él no hay nada que enseñar, pero se tolera si al menos el step tiene
    # ventana: el backend a veces lo completa después.
    calculated = raw.get("time_calculated")
    if calculated is not None and not _is_schedule(calculated):
        return "un punto de control tiene un horario inválido"

    reported = raw.get("time_reported")
    if reported is not None and reported != NOT_REPORTED and not _is_schedule(reported):
        return "un punto de control tiene una hora de llegada inválida"

    return None


def validate_step(raw) -> Optional[str]:
    if not isinstance(raw, dict):
        return "un recorrido no es un objeto"
    if not _is_id(raw.get("step")):
        return "un recorrido no tiene número"
    if not _is_schedule(raw.get("start_schedule")) or not _is_schedule(raw.get("end_schedule")):
        return f"el recorrido {raw.get('step')} no tiene horario utilizable"

    checkpoints = raw.get("checkpoints")
    if not isinstance(checkpoints, list) or not checkpoints:
        return f"el recorrido {raw.get('step')} no tiene puntos de control"

    for ckpt in checkpoints:
        problem = validate_checkpoint(ckpt)
        if problem:
            return f"{problem} (recorrido {raw.get('step')})"

    return None


def validate_dispatches(raw) -> Validation:
    """
    ¿La respuesta del backend remoto puede reemplazar el itinerario?

    Una LISTA VACÍA es válida: significa que el bus no trabaja hoy, y ese sí es
    un estado que debe reflejarse. Cualquier otra forma —no es lista, un step
    sin horario, un checkpoint sin coordenadas— es inválida, y en ese caso el
    itinerario anterior NO se toca.
    """
    if not isinstance(raw, list):
        return Validation(False, "El servidor devolvió un itinerario con un formato inesperado")

    if not raw:
        return Validation(True)

    numbers = set()
    for step in raw:
        problem = validate_step(step)
        if problem:
            return Validation(False, f"El itinerario recibido no es utilizable: {problem}")
        number = step["step"]
        if number in numbers:
            return Validation(False, f"El itinerario recibido repite el recorrido {number}")
        numbers.add(number)

    return Validation(True)


# ─────────────────────────────────────────────
# FUSIÓN DE MARCACIONES LOCALES
# ─────────────────────────────────────────────

@dataclass
class MergeReport:
    """Qué pasó con las marcaciones locales pendientes al fusionar."""
    preserved: list = field(default_factory=list)   # [(step, checkpoint_id)]
    dropped: list = field(default_factory=list)     # no existen en el itinerario nuevo

    @property
    def any_preserved(self) -> bool:
        return bool(self.preserved)


def local_reports(dispatches, pending_ids) -> dict:
    """
    Marcaciones del despacho local que todavía NO se subieron al backend remoto,
    indexadas por su identidad estable `(step, checkpoint_id)`.

    `pending_ids` son los checkpoint_id de la cola local de subida (filas de
    `checkpoint` con upload=False). Solo esas se conservan: una marcación que ya
    viajó al servidor es el servidor quien debe devolverla, y resucitarla aquí
    sería reintroducir un dato que el backend pudo corregir a propósito.
    """
    pending = {cid for cid in (pending_ids or []) if _is_id(cid)}
    found = {}

    if not isinstance(dispatches, list) or not pending:
        return found

    for step in dispatches:
        if not isinstance(step, dict):
            continue
        number = step.get("step")
        if not _is_id(number):
            continue
        checkpoints = step.get("checkpoints")
        if not isinstance(checkpoints, list):
            continue
        for ckpt in checkpoints:
            if not isinstance(ckpt, dict):
                continue
            cid = ckpt.get("id")
            reported = ckpt.get("time_reported")
            if not _is_id(cid) or cid not in pending:
                continue
            if not isinstance(reported, str) or reported in ("", NOT_REPORTED):
                continue
            found[(number, cid)] = reported

    return found


def merge_pending_reports(remote, pending_reports) -> MergeReport:
    """
    Copia las marcaciones locales pendientes sobre el itinerario recién
    descargado. MUTA `remote` (que acaba de llegar de la red y todavía no se ha
    guardado), y devuelve el detalle de lo que hizo.

    Reglas:

      · La identidad es `(step, checkpoint_id)`. Si el itinerario nuevo no tiene
        ese par exacto, la marcación se DESCARTA y se registra — trasladarla al
        checkpoint que ocupe la misma posición la pondría en otro recorrido.
      · Si el itinerario nuevo ya trae una hora para ese checkpoint, gana el
        servidor: es la versión que el resto del sistema va a ver.
    """
    report = MergeReport()
    if not pending_reports:
        return report

    index = {}
    if isinstance(remote, list):
        for step in remote:
            if not isinstance(step, dict) or not _is_id(step.get("step")):
                continue
            for ckpt in step.get("checkpoints") or []:
                if isinstance(ckpt, dict) and _is_id(ckpt.get("id")):
                    index[(step["step"], ckpt["id"])] = ckpt

    for key, reported in pending_reports.items():
        target = index.get(key)
        if target is None:
            report.dropped.append(key)
            log.warning(
                "[REFRESH] La marcación local (step=%s checkpoint=%s) no existe en el "
                "itinerario nuevo — no se traslada a otro recorrido",
                key[0], key[1],
            )
            continue

        existing = target.get("time_reported")
        if isinstance(existing, str) and existing not in ("", NOT_REPORTED):
            continue   # el servidor ya la tiene: manda su versión

        target["time_reported"] = reported
        report.preserved.append(key)

    if report.preserved:
        log.info("[REFRESH] %d marcación(es) local(es) pendiente(s) conservadas", len(report.preserved))

    return report


def count_checkpoints(dispatches) -> int:
    """Total de puntos de control del itinerario. Solo informativo."""
    if not isinstance(dispatches, list):
        return 0
    return sum(
        len(step.get("checkpoints") or [])
        for step in dispatches
        if isinstance(step, dict)
    )
