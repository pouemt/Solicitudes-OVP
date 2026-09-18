import csv
import io
import json
import os
import re
import time
import requests
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
import pandas as pd
from osgeo import ogr, osr


# --- 1. NORMALIZACIÓN DE FECHAS ---
def normalizar_fecha_str(val_fecha):
  """Convierte fechas de Excel (Timestamp o NaT) o cadenas a formato YYYYMMDD."""
  if pd.isna(val_fecha):
    return ""

  if isinstance(val_fecha, (pd.Timestamp, datetime)):
    return val_fecha.strftime("%Y%m%d")

  # Si ya viene como texto (por ejemplo "2026-05-10" o "10/05/2026")
  val_str = str(val_fecha).strip()
  dt = pd.to_datetime(val_str, errors="coerce")
  return dt.strftime("%Y%m%d") if pd.notna(dt) else ""


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


# --- 5. DESCARGA DE EXCEL DESDE SHAREPOINT ---
def descargar_excel_sharepoint(url):
  """Descarga el archivo Excel desde SharePoint/URL usando requests."""
  # .strip() elimina posibles espacios en blanco o saltos de línea al inicio o final
  url = url.strip()

  print(f'Descargando libro Excel desde SharePoint...')

  headers = {
      'User-Agent': (
          'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,'
          ' like Gecko) Chrome/120.0.0.0 Safari/537.36'
      )
  }

  # Realizar la petición GET permitiendo redirecciones
  response = requests.get(
      url, headers=headers, timeout=30, allow_redirects=True
  )
  response.raise_for_status()  # Lanza error si el estado HTTP no es OK (200)

  return io.BytesIO(response.content)

# --- 6. PROCESO PRINCIPAL (EXCEL A ICS, GEOPACKAGE Y KML) ---
def excel_sharepoint_to_ics_gpkg(origen_excel, rutas_destino, ruta_gpkg, ruta_kml):
    lineas_ics = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Ayuntamiento Movilidad//Ocupaciones Via Publica//ES",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]

    datos_para_gpkg = {}

    # Cargar el archivo Excel desde enlace web o ruta local
    if origen_excel.startswith("http://") or origen_excel.startswith("https://"):
        print("Descargando libro Excel desde SharePoint/URL...")
        buffer_excel = descargar_excel_sharepoint(origen_excel)
        df = pd.read_excel(buffer_excel)
    else:
        print(f"Cargando libro Excel local desde: {origen_excel}")
        df = pd.read_excel(origen_excel)

    df = df.fillna("")

    for _, fila in df.iterrows():
        def get_val(col):
            v = fila.get(col, "")
            if pd.isna(v):
                return ""
            return str(v).strip()

        exp_id = get_val("ID")
        # Si Pandas lee el ID como flotante (ej. 1001.0), eliminamos el decimal
        if exp_id.endswith(".0"):
            exp_id = exp_id[:-2]

        if not exp_id:
            continue

        servei = get_val("SERVEI")
        tecnic = get_val("TECNIC")
        contratista = get_val("CONTRATISTA")
        cap_obra = get_val("CAP OBRA")
        emplazamiento = get_val("EMPLAZAMIENTO")
        descripcion = get_val("DESCRIPCIO OBRA")
        tipo_obra = get_val("TIPUS OBRA")
        observaciones = get_val("OBSERVACIONS")
        condicions_mobilitat = get_val("CONDICIONS MOBILITAT")

        f_inicio = normalizar_fecha_str(fila.get("DATA INICI"))
        f_fin = normalizar_fecha_str(fila.get("DATA FINALITZACIO"))

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

    # Exportación a KML
    exportar_geopackage_a_kml(ruta_gpkg, ruta_kml)


# --- 7. RUTAS Y CONFIGURACIÓN DE EJECUCIÓN ---
url_excel_sharepoint = "https://ajtpalma-my.sharepoint.com/:x:/g/personal/pedro_pourtau_palma_es/IQDgISBCy3jTRJZpXC5jRRflAYITwYojKDvs50WStuiJd90?rtime=YsdeEnAV30g&nav=MTVfezAwMDAwMDAwLTAwMDEtMDAwMC0wMDAwLTAwMDAwMDAwMDAwMH0&download=1"
ruta_ics = "OCUPACION_VIA_PUBLICA.ics"
ruta_gpkg = "OCUPACION_VIA_PUBLICA.gpkg"
ruta_kml = "OCUPACION_VIA_PUBLICA.kml"

rutas_destino = [ruta_ics]

# Ejecución principal leyendo directamente del Excel de SharePoint
excel_sharepoint_to_ics_gpkg(url_excel_sharepoint, rutas_destino, ruta_gpkg, ruta_kml)
