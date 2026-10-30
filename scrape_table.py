import json
import uuid
from datetime import datetime

import boto3
import requests

# El ejercicio solicita solamente los 10 sismos más recientes.
LIMITE = 10
URL = 'https://ultimosismo.igp.gob.pe/api/ultimo-sismo/ajaxb/{anio}'
TABLA = 'sismos-igp-dev'  # Tabla que ya crea nuestro serverless.yml
HEADERS = {
    'User-Agent': 'Mozilla/5.0',
    'Accept': 'application/json, */*',
    'Referer': 'https://ultimosismo.igp.gob.pe/productos/reportes-sismicos'
}


def respuesta(estado, datos):
    # En la integración Lambda Proxy de API Gateway, body debe ser texto.
    return {
        'statusCode': estado,
        'headers': {'Content-Type': 'application/json; charset=utf-8'},
        'body': json.dumps(datos, ensure_ascii=False)
    }


def obtener_sismos(anio):
    response = requests.get(URL.format(anio=anio), headers=HEADERS, timeout=20)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict):
        data = data.get('data') or data.get('sismos') or []
    if not isinstance(data, list):
        raise ValueError('El IGP no devolvió una lista de sismos')
    return data


def clave_orden(s):
    """Ordena primero los sismos más recientes por fecha y hora locales."""
    fecha = str(s.get('fecha_local') or s.get('fecha_utc') or '').strip()[:10]
    hora = str(s.get('hora_local') or s.get('hora_utc') or '').strip()
    if 'T' in hora:
        hora = hora.split('T', 1)[1]
    hora = hora[:8] or '00:00:00'

    for formato in ('%Y-%m-%d %H:%M:%S', '%d/%m/%Y %H:%M:%S'):
        try:
            return (datetime.strptime(fecha + ' ' + hora, formato),
                    str(s.get('codigo') or s.get('reporte') or s.get('id') or ''))
        except ValueError:
            continue
    return (datetime.min, str(s.get('codigo') or s.get('reporte') or s.get('id') or ''))


def a_item(sismo):
    """Convertir los datos del IGP a tipos compatibles con DynamoDB."""
    item = {}
    for k, v in sismo.items():
        if v is None or v == '':
            continue
        item[k] = json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else str(v)
    return item


def lambda_handler(event, context):
    # Paso 1. Obtener los sismos del IGP en el año actual.
    anio = datetime.now().year
    try:
        sismos = obtener_sismos(anio)
    except (requests.RequestException, ValueError) as error:
        return respuesta(502, {'error': 'No se pudo acceder a los datos del IGP',
                               'detalle': str(error)})

    if not sismos:
        return respuesta(404, {'error': 'No se encontraron sismos en la respuesta'})

    # Paso 2. Ordenar y escoger 10 reportes distintos.
    ordenados = sorted(sismos, key=clave_orden, reverse=True)
    rows = []
    codigos = set()
    for sismo in ordenados:
        if not isinstance(sismo, dict):
            continue
        codigo = str(sismo.get('codigo') or sismo.get('reporte') or
                     sismo.get('cod_reporte') or sismo.get('id') or '').strip()
        if not codigo or codigo in codigos:
            continue
        codigos.add(codigo)

        row = a_item(sismo)
        row['codigo'] = codigo  # Clave primaria de sismos-igp-dev
        row['#'] = len(rows) + 1
        if 'id' in row:
            row['id_igp'] = row['id']  # Conservar el ID original, si existe.
        row['id'] = str(uuid.uuid4())  # Igual que en el ejemplo del profesor.
        rows.append(row)
        if len(rows) == LIMITE:
            break

    if len(rows) != LIMITE:
        return respuesta(422, {'error': 'No se encontraron 10 sismos con identificador',
                               'encontrados': len(rows)})

    # Paso 3. Limpiar la tabla y guardar los 10 reportes más recientes.
    try:
        dynamodb = boto3.resource('dynamodb')
        table = dynamodb.Table(TABLA)

        scan = table.scan(ProjectionExpression='codigo')
        with table.batch_writer() as batch:
            while True:
                for each in scan['Items']:
                    batch.delete_item(Key={'codigo': each['codigo']})
                if 'LastEvaluatedKey' not in scan:
                    break
                scan = table.scan(ProjectionExpression='codigo',
                                  ExclusiveStartKey=scan['LastEvaluatedKey'])

        with table.batch_writer() as batch:
            for row in rows:
                batch.put_item(Item=row)
    except Exception as error:
        return respuesta(500, {'error': 'No se pudieron guardar los sismos en DynamoDB',
                               'detalle': str(error)})

    # Paso 4. Devolver una respuesta JSON compatible con API Gateway.
    return respuesta(200, {
        'mensaje': 'Sismos almacenados correctamente',
        'total': len(rows),
        'tabla': TABLA,
        'sismos': rows
    })
