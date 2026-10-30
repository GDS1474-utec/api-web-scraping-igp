import requests
from bs4 import BeautifulSoup
import boto3
import json
import re
from datetime import datetime, timezone
from decimal import Decimal

# Página indicada en el Ejercicio 2 del taller
URL = 'https://ultimosismo.igp.gob.pe/productos/reportes-sismicos'
# Fuente oficial alternativa, por si la tabla se carga con JavaScript
URL_DATOS = ('https://ide.igp.gob.pe/arcgis/rest/services/'
             'monitoreocensis/SismosReportados/MapServer/0/query')
TABLA_DYNAMODB = 'sismos-igp-dev'


def respuesta(estado, datos):
    return {
        'statusCode': estado,
        'headers': {'Content-Type': 'application/json; charset=utf-8'},
        'body': json.dumps(datos, ensure_ascii=False)
    }


def fecha_orden(sismo):
    return datetime.strptime(sismo['fecha_local'], '%d/%m/%Y %H:%M:%S')


def extraer_tabla_html(contenido):
    # Mismo procedimiento del profesor: BeautifulSoup, tabla, filas y celdas.
    soup = BeautifulSoup(contenido, 'html.parser')
    table = soup.find('table')
    if not table:
        return []

    rows = []
    for row in table.find_all('tr'):
        cells = [cell.get_text(' ', strip=True) for cell in row.find_all('td')]
        # Columnas de la página: #, Cod reporte, Referencia,
        # Semáforo, Magnitud, Fecha y hora de origen local, Ver reporte.
        for i, celda in enumerate(cells):
            if 'IGP/CENSIS/RS' not in celda or len(cells) < i + 5:
                continue
            try:
                codigo = celda.strip()
                referencia = cells[i + 1].strip()
                semaforo = cells[i + 2].strip()
                magnitud = float(cells[i + 3].replace(',', '.'))
                fecha_local = cells[i + 4].strip()
                datetime.strptime(fecha_local, '%d/%m/%Y %H:%M:%S')
                rows.append({
                    'codigo': codigo,
                    'referencia': referencia,
                    'semaforo': semaforo,
                    'magnitud': magnitud,
                    'fecha_local': fecha_local
                })
            except (ValueError, IndexError):
                continue
            break

    # Evitar registros repetidos y devolver los más recientes primero.
    rows = list({row['codigo']: row for row in rows}.values())
    return sorted(rows, key=fecha_orden, reverse=True)[:10]


def extraer_datos_igp():
    # La web del IGP puede mostrar la tabla solo después de ejecutar JavaScript.
    # En ese caso, se consultan los datos publicados por el propio IGP.
    response = requests.get(URL_DATOS, params={
        'where': '1=1',
        'outFields': 'code,ref,magnitud,fecha,hora,fechaevento',
        'orderByFields': 'fecha DESC',
        'resultRecordCount': 50,
        'returnGeometry': 'false',
        'f': 'json'
    }, timeout=12)
    response.raise_for_status()
    datos = response.json()
    if 'error' in datos:
        raise ValueError(str(datos['error']))

    rows = []
    for feature in datos.get('features', []):
        campos = feature.get('attributes', {})
        codigo = re.search(r'\d{4}-\d{3,5}', str(campos.get('code', '')))
        fecha_ms = campos.get('fecha') or campos.get('fechaevento')
        if not codigo or fecha_ms is None or campos.get('magnitud') is None:
            continue
        try:
            # El campo 'fecha' de ArcGIS es una fecha en milisegundos.
            fecha = datetime.fromtimestamp(fecha_ms / 1000, tz=timezone.utc)
            hora = re.search(r'\d{1,2}:\d{2}:\d{2}', str(campos.get('hora', '')))
            fecha_local = fecha.strftime('%d/%m/%Y') + ' ' + (
                hora.group().zfill(8) if hora else fecha.strftime('%H:%M:%S'))
            magnitud = float(campos['magnitud'])
            rows.append({
                'codigo': 'IGP/CENSIS/RS ' + codigo.group(),
                'referencia': str(campos.get('ref') or 'No disponible').strip(),
                'semaforo': 'No disponible',  # No figura en este servicio de datos.
                'magnitud': magnitud,
                'fecha_local': fecha_local
            })
        except (TypeError, ValueError, OverflowError):
            continue

    rows = list({row['codigo']: row for row in rows}.values())
    return sorted(rows, key=fecha_orden, reverse=True)[:10]


def lambda_handler(event, context):
    # Realizar solicitud HTTP a la página del IGP.
    try:
        response = requests.get(URL, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
        rows = extraer_tabla_html(response.content)
        fuente = 'pagina_igp'
    except requests.RequestException:
        rows = []
        fuente = 'servicio_igp'

    # Si el HTML no trae los 10 sismos, consultar servicio oficial del IGP.
    if len(rows) < 10:
        try:
            rows = extraer_datos_igp()
            fuente = 'servicio_igp'
        except (requests.RequestException, ValueError, KeyError, TypeError) as error:
            return respuesta(502, {'error': 'No se pudo consultar el IGP',
                                   'detalle': str(error)})

    if len(rows) < 10:
        return respuesta(502, {'error': 'No se encontraron 10 sismos válidos',
                               'cantidad': len(rows)})

    # Guardar en la tabla DynamoDB creada mediante Serverless.
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(TABLA_DYNAMODB)

    try:
        # Igual que el profesor: limpiar los registros anteriores.
        scan = table.scan(ProjectionExpression='codigo')
        with table.batch_writer() as batch:
            while True:
                for each in scan.get('Items', []):
                    batch.delete_item(Key={'codigo': each['codigo']})
                if 'LastEvaluatedKey' not in scan:
                    break
                scan = table.scan(
                    ProjectionExpression='codigo',
                    ExclusiveStartKey=scan['LastEvaluatedKey']
                )

        # Insertar los 10 nuevos registros.
        with table.batch_writer() as batch:
            for row in rows:
                item = dict(row)
                item['magnitud'] = Decimal(str(row['magnitud']))
                batch.put_item(Item=item)
    except Exception as error:
        return respuesta(500, {'error': 'No se pudieron guardar los sismos en DynamoDB',
                               'detalle': str(error)})

    return respuesta(200, {
        'mensaje': '10 sismos guardados correctamente',
        'cantidad': len(rows),
        'tabla': TABLA_DYNAMODB,
        'fuente': fuente,
        'sismos': rows
    })
