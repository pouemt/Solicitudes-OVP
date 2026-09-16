import csv
import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from osgeo import ogr, osr


# --- 1. NORMALIZACIÓN DE FECHAS ---
def normalizar_fecha_obj(f_str):
    f_str = f_str.strip()
    if not f_str:
        return None

    if f_str.isdigit():
        if len(f_str) == 8:
            if f_str.startswith("20"):
                return datetime.strptime(f_str, "%Y%m%d")
            return datetime.strptime(f_str, "%d%m%Y")
        elif len(f_str) == 6:
            return datetime.strptime(f_str, "%d%m%y")

    formatos_posibles = [
        "%d/%m/%Y",
        "%d/%m/%y",
        "%d-%m-%Y",
        "%d-%m-%y",
        "%Y-%m-%d",
        "%Y/%m/%d",
    ]

    for formato in formatos_posibles:
        try:
            return datetime.strptime(f_str, formato)
        except ValueError:
            continue

    return None


# --- 2. FUNCIONES DE GEOCODIFICACIÓN CON NOMINATIM ---
def parsear_direccion_interseccion(direccion):
    """Detecta y formatea cruces de calles para Nominatim (ej. 'Calle A & Calle B')."""
    patron = r"(?:intersección|esquina|cruce|confluencia)\s+(?:de\s+la\s+|del?\s+)?(?:calle\s+|c/\s*)?(.+?)\s+(?:con|y|esquina)\s+(?:la\s+calle\s+|c/\s*)?(.+)"
    coincidencia = re.search(patron, direccion, re.IGNORECASE)
    if coincidencia:
        calle1 = coincidencia.group(1).strip()
        calle2 = coincidencia.group(2).strip()
        return f"{calle1} & {calle2}"
    return direccion


def consultar_nominatim(texto_busqueda):
    """Realiza la petición HTTP a la API pública de Nominatim."""
    url = "https://nominatim.openstreetmap.org/search?" + urllib.parse.urlencode(
        {"q": texto_busqueda, "format": "json", "limit": 1}
    )
    req = urllib.request.Request(
        url, headers={"User-Agent": "QGIS_OVP_Script/1.0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=3) as response:
            datos = json.loads(response.read().decode())
            if datos:
                return float(datos[0]["lon"]), float(datos[0]["lat"])
    except Exception:
        pass
    return None, None


def obtener_coordenadas_robustas(direccion_raw, ciudad="Palma, España"):
    """Estrategia de geocodificación en 3 pasos: Exacta/Cruces -> Punto medio -> Pendiente."""
    if not direccion_raw:
        return None, None, "PENDIENTE"

    direccion_limpia = parsear_direccion_interseccion(direccion_raw)

    # Paso 1: Búsqueda directa o intersección exacta
    lon, lat = consultar_nominatim(f"{direccion_limpia}, {ciudad}")
    if lon and lat:
        return lon, lat, "EXACTO"

    time.sleep(1)  # Respetar políticas de uso de la API gratuita

    # Paso 2: Fallback para cruces no detectados directamente (Punto medio entre calles)
    if "&" in direccion_limpia:
        calle1, calle2 = direccion_limpia.split("&")
        lon1, lat1 = consultar_nominatim(f"{calle1.strip()}, {ciudad}")
        time.sleep(1)
        lon2, lat2 = consultar_nominatim(f"{calle2.strip()}, {ciudad}")

        if lon1 and lon2:
            return (lon1 + lon2) / 2, (lat1 + lat2) / 2, "APROXIMADO"

    # Paso 3: Registro sin ubicación precisa
    return None, None, "PENDIENTE"


# --- 3. ACTUALIZACIÓN Y CREACIÓN EN GEOPACKAGE VÍA OGR ---
def actualizar_geopackage_ogr(ruta_gpkg, datos_para_gpkg):
    if not os.path.exists(ruta_gpkg):
        print(f"Aviso: No se encuentra el GeoPackage en {ruta_gpkg}")
        return

    ds = ogr.Open(ruta_gpkg, 1)
    if not ds:
        print("Error: No se pudo abrir el GeoPackage para escritura.")
        return

    capa = ds.GetLayerByName("ocupaciones_tramos")
    if not capa:
        print("Error: No se encontró la capa 'ocupaciones_tramos'.")
        ds = None
        return

    # Reproyección automática si la capa usa un CRS distinto a WGS84 (ej. ETRS89 / UTM 31N - EPSG:25831)
    target_srs = capa.GetSpatialRef()
    transform = None
    if target_srs:
        source_srs = osr.SpatialReference()
        source_srs.ImportFromEPSG(4326)
        source_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        if not source_srs.IsSame(target_srs):
            transform = osr.CoordinateTransformation(source_srs, target_srs)

    expedientes_en_gpkg = set()
    registros_actualizados = 0
    registros_nuevos = 0

    # 1. Recorrer y actualizar geometrías existentes
    for feature in capa:
        exp_id = (
            str(feature.GetField("ID")).strip()
            if feature.GetField("ID")
            else ""
        )
        if exp_id:
            expedientes_en_gpkg.add(exp_id)

        if exp_id in datos_para_gpkg:
            (
                serv,
                tec,
                contr,
                cap,
                emplaz,
                descr,
                tip,
                obs,
                condmob,
                fini,
                fend,
            ) = datos_para_gpkg[exp_id]

            feature.SetField("servei", serv)
            feature.SetField("tecnic", tec)
            feature.SetField("contratista", contr)
            feature.SetField("cap_obra", cap)
            feature.SetField("emplazamiento", emplaz)
            feature.SetField("descripcion", descr)
            feature.SetField("tipo_obra", tip)
            feature.SetField("observaciones", obs)
            feature.SetField("condicions_mobilitat", condmob)
            feature.SetField("f_inicio", fini)
            feature.SetField("f_fin", fend)

            # Si el elemento no tiene geometría aún, intentar geocodificar
            if not feature.GetGeometryRef():
                lon, lat, estado_geo = obtener_coordenadas_robustas(emplaz)
                feature.SetField("estado_geo", estado_geo)
                if lon and lat:
                    punto = ogr.Geometry(ogr.wkbPoint)
                    punto.AddPoint(lon, lat)
                    if transform:
                        punto.Transform(transform)
                    feature.SetGeometry(punto)

            capa.SetFeature(feature)
            registros_actualizados += 1

    # 2. Insertar registros nuevos con geocodificación
    defn = capa.GetLayerDefn()
    for exp_id, (
        serv,
        tec,
        contr,
        cap,
        emplaz,
        descr,
        tip,
        obs,
        condmob,
        fini,
        fend,
    ) in datos_para_gpkg.items():
        if exp_id not in expedientes_en_gpkg:
            new_feature = ogr.Feature(defn)

            new_feature.SetField("ID", exp_id)
            new_feature.SetField("servei", serv)
            new_feature.SetField("tecnic", tec)
            new_feature.SetField("contratista", contr)
            new_feature.SetField("cap_obra", cap)
            new_feature.SetField("emplazamiento", emplaz)
            new_feature.SetField("descripcion", descr)
            new_feature.SetField("tipo_obra", tip)
            new_feature.SetField("observaciones", obs)
            new_feature.SetField("condicions_mobilitat", condmob)
            new_feature.SetField("f_inicio", fini)
            new_feature.SetField("f_fin", fend)

            # Geocodificar ubicación
            lon, lat, estado_geo = obtener_coordenadas_robustas(emplaz)
            new_feature.SetField("estado_geo", estado_geo)

            if lon and lat:
                punto = ogr.Geometry(ogr.wkbPoint)
                punto.AddPoint(lon, lat)
                if transform:
                    punto.Transform(transform)
                new_feature.SetGeometry(punto)

            capa.CreateFeature(new_feature)
            registros_nuevos += 1

    ds = None
    print(
        f"GeoPackage actualizado: {registros_actualizados} modificados, {registros_nuevos} nuevos procesados."
    )

# --- 4. EXPORTAR GEOPACKAGE A KML ---
def exportar_geopackage_a_kml(ruta_gpkg, ruta_kml):
    driver_gpkg = ogr.GetDriverByName("GPKG")
    driver_kml = ogr.GetDriverByName("KML")

    ds_in = driver_gpkg.Open(ruta_gpkg, 0)
    if not ds_in:
        print("Error al abrir el GeoPackage para exportación a KML.")
        return

    capa_in = ds_in.GetLayerByName("ocupaciones_tramos")
    if not capa_in:
        print("Error: No se encontró la capa 'ocupaciones_tramos'.")
        ds_in = None
        return

    # Obtener la fecha actual en el mismo formato en que se guarda en la BD (YYYYMMDD)
    fecha_hoy = datetime.now().strftime("%Y%m%d")

    # Aplicar filtro OGR: solo registros cuya fecha de fin sea hoy o posterior
    capa_in.SetAttributeFilter(f"f_fin >= '{fecha_hoy}'")

    # Eliminar KML previo si existe para sobreescribir
    if os.path.exists(ruta_kml):
        driver_kml.DeleteDataSource(ruta_kml)

    # Copiar únicamente las entidades que cumplen el filtro al nuevo KML
    ds_out = driver_kml.CreateDataSource(ruta_kml)
    ds_out.CopyLayer(capa_in, "ocupaciones_tramos")

    ds_in = None
    ds_out = None
    print(
        f"Éxito: KML exportado con ocupaciones activas a partir de {fecha_hoy}."
    )


# --- 5. PROCESO PRINCIPAL ---
def csv_to_ics_gpkg(archivo_csv, rutas_destino, ruta_gpkg):
    lineas_ics = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Ayuntamiento Movilidad//Ocupaciones Via Publica//ES",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]

    datos_para_gpkg = {}

    with open(archivo_csv, mode="r", encoding="utf-8-sig") as f:
        muestra = f.read(2048)
        f.seek(0)
        delimitador = ";" if ";" in muestra else "\t"

        lector = csv.DictReader(f, delimiter=delimitador)

        for fila in lector:
            exp_id = fila.get("ID", "").strip()
            if not exp_id:
                continue

            servei = fila.get("SERVEI", "").strip()
            tecnic = fila.get("TECNIC", "").strip()
            contratista = fila.get("CONTRATISTA", "").strip()
            cap_obra = fila.get("CAP OBRA", "").strip()
            emplazamiento = fila.get("EMPLAZAMIENTO", "").strip()
            descripcion = fila.get("DESCRIPCIO OBRA", "").strip()
            tipo_obra = fila.get("TIPUS OBRA", "").strip()
            observaciones = fila.get("OBSERVACIONS", "").strip()
            condicions_mobilitat = fila.get("CONDICIONS MOBILITAT", "").strip()

            f_inicio = normalizar_fecha_obj(fila.get("DATA INICI", ""))
            f_fin = normalizar_fecha_obj(fila.get("DATA FINALITZACIO", ""))

            if f_inicio and f_fin:
                f_fin_ampliada = f_fin + timedelta(days=1)
                f_inicio_str = f_inicio.strftime("%Y%m%d")
                f_fin_str = f_fin_ampliada.strftime("%Y%m%d")

                # A) Bloques para los calendarios .ics
                lineas_ics.extend([
                    "BEGIN:VEVENT",
                    f"UID:ocupacion-{exp_id}",
                    f"SUMMARY:{emplazamiento}",
                    f"DESCRIPTION:{tipo_obra}-{descripcion}-{observaciones}",
                    f"DTSTART;VALUE=DATE:{f_inicio_str}",
                    f"DTEND;VALUE=DATE:{f_fin_str}",
                    "END:VEVENT",
                ])

                # B) Tupla de atributos para el GeoPackage
                datos_para_gpkg[exp_id] = (
                    servei,
                    tecnic,
                    contratista,
                    cap_obra,
                    emplazamiento,
                    descripcion,
                    tipo_obra,
                    observaciones,
                    condicions_mobilitat,
                    f_inicio_str,
                    f_fin.strftime("%Y%m%d"),
                )

    lineas_ics.append("END:VCALENDAR")

    # Escritura de archivos .ics
    for ruta in rutas_destino:
        with open(ruta, mode="w", encoding="utf-8") as f_out:
            f_out.write("\n".join(lineas_ics))
            print(f"Éxito: Archivo '{ruta}' generado correctamente.")

    # Sincronización con el GeoPackage
    actualizar_geopackage_ogr(ruta_gpkg, datos_para_gpkg)

    # Exportacion a KLM
    exportar_geopackage_a_kml(ruta_gpkg, ruta_kml)


# --- RUTAS DE EJECUCIÓN ---
ruta_csv = "BD_OCUPACION_VIA_PUBLICA.csv"
ruta_ics = "OCUPACION_VIA_PUBLICA.ics"
ruta_gpkg = "OCUPACION_VIA_PUBLICA.gpkg"
ruta_kml = "OCUPACION_VIA_PUBLICA.kml"

rutas_destino = [ruta_ics]
csv_to_ics_gpkg(ruta_csv, rutas_destino, ruta_gpkg)
