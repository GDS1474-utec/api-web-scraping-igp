import requests
from bs4 import BeautifulSoup
import boto3
import json
from decimal import Decimal

# Ejercicio 2: 10 últimos reportes sísmicos del IGP
URL = 'https://ultimosismo.igp.gob.pe/productos/reportes-sismicos'
TABLA = 'sismos-igp-dev'


def respuesta(codigo, mensaje):
    return {
        'statusCode': codigo,
        'headers': {'Content-Type': 'application/json; charset=utf-8'},
        'body': json.dumps(mensaje, ensure_ascii=False)
    }


def lambda_handler(event, context):
    # Paso 1: Consultar la página del IGP, como en el ejemplo del profesor.
    try:
        response = requests.get(URL, timeout=15, headers={'User-Agent': 'Mozilla/5.0'})
        response.raise_for_status()
    except requests.RequestException as error:
        return respuesta(502, {'error': 'No se pudo acceder a la web del IGP', 'detalle': str(error)})

    # Paso 2: Parsear el HTML y localizar el <tbody> de la tabla.
    soup = BeautifulSoup(response.content, 'html.parser')
    tbody = soup.select_one('table tbody')

    if tbody is None:
        return respuesta(404, {
            'error': 'No se encontró tbody en el HTML descargado',
            'detalle': ('Si aparece en Inspeccionar elemento pero no aquí, '
                        'es posible que JavaScript agregue las filas en el navegador.')
        })

    # Paso 3: Extraer cada fila <tr> y sus celdas <td>.
    # Orden de las columnas: #, código, referencia, semáforo, magnitud,
    # fecha y hora local, enlace para ver reporte.
    rows = []
    for tr in tbody.find_all('tr', recursive=False)[:10]:
        celdas = tr.find_all('td', recursive=False)
        if len(celdas) < 6:
            continue

        valores = [td.get_text(' ', strip=True) for td in celdas]
        codigo = valores[1]
        if not codigo.startswith('IGP/CENSIS/RS'):
            continue
        try:
            magnitud = Decimal(valores[4].replace(',', '.'))
        except Exception:
            continue

        rows.append({
            'codigo': codigo,
            'referencia': valores[2],
            'semaforo': valores[3],
            'magnitud': magnitud,
            'fecha_local': valores[5]
        })

    if len(rows) != 10:
        return respuesta(422, {
            'error': 'No se pudieron extraer los 10 sismos desde tbody',
            'cantidad_encontrada': len(rows),
            'detalle': 'No se modificó DynamoDB.'
        })

    # Paso 4: Guardar los 10 sismos en DynamoDB.
    # La clave primaria existente en serverless.yml es "codigo" (no "id").
    dynamodb = boto3.resource('dynamodb')
    table = dynamodb.Table(TABLA)

    try:
        scan = table.scan(ProjectionExpression='codigo')
        with table.batch_writer() as batch:
            while True:
                for item in scan.get('Items', []):
                    batch.delete_item(Key={'codigo': item['codigo']})
                if 'LastEvaluatedKey' not in scan:
                    break
                scan = table.scan(
                    ProjectionExpression='codigo',
                    ExclusiveStartKey=scan['LastEvaluatedKey']
                )

            for row in rows:
                batch.put_item(Item=row)
    except Exception as error:
        return respuesta(500, {'error': 'Error al guardar en DynamoDB', 'detalle': str(error)})

    # Convertir Decimal para serializar la respuesta HTTP.
    resultados = [{**row, 'magnitud': float(row['magnitud'])} for row in rows]
    return respuesta(200, {
        'mensaje': '10 sismos guardados correctamente',
        'cantidad': len(rows),
        'tabla': TABLA,
        'sismos': resultados
    })
