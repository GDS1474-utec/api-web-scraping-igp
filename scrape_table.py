"""Ejercicio 2: scrape de los 10 ultimos reportes sismicos del IGP y escritura en DynamoDB."""

import json
import logging
import os
import re
from datetime import datetime, timezone
from decimal import Decimal

import boto3
import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

URL_IGP = "https://ultimosismo.igp.gob.pe/productos/reportes-sismicos"
CODIGO_RE = re.compile(r"IGP/CENSIS/RS\s+(\d{4}-\d{3,5})", re.I)
FECHA_RE = re.compile(r"\b\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2}\b")
MAG_RE = re.compile(r"^\d{1,2}(?:[.,]\d+)?$")
SEMAFORO_RE = re.compile(r"\b(verde|amarillo|rojo)\b", re.I)


def _limpiar(texto):
    return " ".join(str(texto).split())


def _armar_sismo(codigo, referencia, semaforo, magnitud, fecha_local):
    """Devuelve un registro solo si sus campos esenciales son validos."""
    if not (codigo and referencia and magnitud and fecha_local):
        return None
    try:
        momento = datetime.strptime(_limpiar(fecha_local), "%d/%m/%Y %H:%M:%S")
        valor_magnitud = float(str(magnitud).replace(",", "."))
    except ValueError:
        return None

    clave = CODIGO_RE.search(codigo)
    if not clave:
        return None
    return {
        "codigo": f"IGP/CENSIS/RS {clave.group(1)}",
        "referencia": _limpiar(referencia),
        "semaforo": semaforo.lower() if semaforo else None,
        "magnitud": valor_magnitud,
        "fecha_local": momento.strftime("%d/%m/%Y %H:%M:%S"),
        "url_reporte": f"https://ultimosismo.igp.gob.pe/evento/{clave.group(1)}",
    }


def _leer_filas_de_tabla(soup):
    """Alternativa 1: web renderizada con filas <tr> y celdas <td>."""
    for tr in soup.select("tr"):
        celdas = [_limpiar(c.get_text(" ", strip=True)) for c in tr.find_all(["td", "th"], recursive=False)]
        indice = next((i for i, c in enumerate(celdas) if CODIGO_RE.search(c)), None)
        if indice is None:
            continue
        codigo = celdas[indice]
        posteriores = celdas[indice + 1:]
        referencia = next((c for c in posteriores if c and not SEMAFORO_RE.fullmatch(c) and not MAG_RE.fullmatch(c)
                           and not FECHA_RE.fullmatch(c) and "reporte" not in c.lower()), None)
        semaforo = next((SEMAFORO_RE.search(c).group(1) for c in posteriores if SEMAFORO_RE.search(c)), None)
        magnitud = next((c for c in posteriores if MAG_RE.fullmatch(c)), None)
        fecha = next((FECHA_RE.search(c).group() for c in posteriores if FECHA_RE.search(c)), None)
        registro = _armar_sismo(codigo, referencia, semaforo, magnitud, fecha)
        if registro:
            yield registro


def _leer_fichas_de_texto(soup):
    """Alternativa 2: fichas con etiquetas (Cod reporte, Referencia, Magnitud, etc.)."""
    # Extraer texto visible; nunca leer scripts JSON como si fueran fichas HTML.
    for etiqueta in soup(["script", "style", "noscript"]):
        etiqueta.decompose()
    texto = _limpiar(" ".join(soup.stripped_strings))
    codigos = list(CODIGO_RE.finditer(texto))
    patron = re.compile(
        r"Referencia\s*:?\s*(.*?)\s*"
        r"Sem[aá]foro\s*:?\s*(verde|amarillo|rojo)\s*"
        r"Magnitud\s*:?\s*(\d{1,2}(?:[.,]\d+)?)\s*"
        r"Fecha y hora de origen local\s*:?\s*(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2}:\d{2})",
        flags=re.I,
    )
    for i, codigo in enumerate(codigos):
        fin = codigos[i + 1].start() if i + 1 < len(codigos) else len(texto)
        bloque = texto[codigo.end():fin]
        hallazgo = patron.search(bloque)
        if not hallazgo:
            continue
        referencia, semaforo, magnitud, fecha = hallazgo.groups()
        registro = _armar_sismo(codigo.group(), referencia, semaforo, magnitud, fecha)
        if registro:
            yield registro


def extraer_sismos(html):
    """Extrae EXACTAMENTE los 10 reportes mas recientes del HTML disponible."""
    soup = BeautifulSoup(html, "html.parser")
    registros = {}
    for registro in _leer_filas_de_tabla(soup):
        registros[registro["codigo"]] = registro
    for registro in _leer_fichas_de_texto(soup):
        registros.setdefault(registro["codigo"], registro)
    ordenados = sorted(
        registros.values(),
        key=lambda r: datetime.strptime(r["fecha_local"], "%d/%m/%Y %H:%M:%S"),
        reverse=True,
    )
    if len(ordenados) < 10:
        raise ValueError(
            f"Solo se encontraron {len(ordenados)} reportes válidos en el HTML. "
            "Verifica si el IGP cambió el HTML o carga los datos mediante JavaScript."
        )
    return ordenados[:10]


def _respuesta(estado, contenido):
    return {
        "statusCode": estado,
        "headers": {"Content-Type": "application/json; charset=utf-8", "Access-Control-Allow-Origin": "*"},
        "body": json.dumps(contenido, ensure_ascii=False),
    }


def lambda_handler(event, context):
    tabla_nombre = os.environ.get("TABLE_NAME")
    if not tabla_nombre:
        return _respuesta(500, {"error": "Falta la variable de entorno TABLE_NAME"})

    try:
        respuesta = requests.get(
            URL_IGP,
            headers={"User-Agent": "Mozilla/5.0 (compatible; TallerAcademico/1.0)"},
            timeout=15,
        )
        respuesta.raise_for_status()
        sismos = extraer_sismos(respuesta.text)
    except (requests.RequestException, ValueError) as exc:
        logger.exception("No se pudo extraer la información del IGP")
        return _respuesta(502, {"error": "No se pudo extraer la información del IGP", "detalle": str(exc)})

    try:
        tabla = boto3.resource("dynamodb").Table(tabla_nombre)
        fecha_consulta = datetime.now(timezone.utc).isoformat()
        for sismo in sismos:
            item = {**sismo, "magnitud": Decimal(str(sismo["magnitud"])), "fecha_consulta_utc": fecha_consulta}
            # La clave codigo evita duplicados en llamadas sucesivas.
            tabla.put_item(Item=item)
    except Exception:
        logger.exception("Error al guardar reportes en DynamoDB")
        return _respuesta(500, {"error": "No se pudieron guardar los reportes en DynamoDB"})

    return _respuesta(200, {"mensaje": "Sismos almacenados correctamente", "cantidad": len(sismos), "tabla": tabla_nombre, "sismos": sismos})
