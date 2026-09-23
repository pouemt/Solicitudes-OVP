import io
import json
import os
import re
import time
import urllib.parse
from datetime import datetime, timedelta
import pandas as pd
import requests
from osgeo import ogr, osr

LON_DEFECTO, LAT_DEFECTO = 2.6400, 39.5555


def normalizar_fecha_obj(val):
    """Normaliza fechas aceptando objetos datetime, Timestamp de Pandas, cadenas o números."""
    if val is None or pd.isna(val):
        return None
    if isinstance(val, (datetime, pd.Timestamp)):
        return val

    f_str = str(val).strip()
    if not f_str or f_str.lower() in ("nan", "none", "nat"):
        return None

    # Normalización de cadenas numéricas compactas (ej. 20260918 o 18092026)
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
        "%Y-%m-%d %H:%M:%S",
    ]

    for formato in formatos_posibles:
        try:
            return datetime.strptime(f_str, formato)
        except ValueError:
            continue

    # Fallback utilizando pandas to_datetime
    try:
        dt = pd.to_datetime(f_str, dayfirst=True, errors="coerce")
        if pd.notna(dt):
            return dt
    except Exception:
        pass

    return None


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
    headers = {"User-Agent": "QGIS_OVP_Script/1.0"}
    try:
        response = requests.get(url, headers=headers, timeout=5)
        if response.status_code == 200:
            datos = response.json()
            if datos:
                return float(datos[0]["lon"]), float(datos[0]["lat"])
    except Exception:
        pass
    return None, None


def obtener_coordenadas_robustas(direccion_raw, ciudad="Palma, España"):
    """Estrategia de geocodificación en 3 pasos: Exacta/Cruces -> Punto medio -> Pendiente."""
    if not direccion_raw:
        return LON_DEFECTO, LAT_DEFECTO, "PENDIENTE"

    direccion_limpia = parsear_direccion_interseccion(direccion_raw)

    # Paso 1: Búsqueda directa o intersección exacta
    lon, lat = consultar_nominatim(f"{direccion_limpia}, {ciudad}")
    if lon and lat:
        return lon, lat, "EXACTO"

    time.sleep(1)  # Respetar políticas de uso de la API gratuita (máx. 1 req/seg)

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


def actualizar_geopackage_ogr(ruta_gpkg, datos_para_gpkg, forzar_recalculo=False):
    """Actualiza, inserta y elimina entidades en la capa GeoPackage usando OGR."""
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

    # Reproyección automática si la capa usa un CRS distinto a WGS84
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
    fids_a_eliminar = []

    # Recorrido de las entidades existentes en la capa GeoPackage
    for feature in capa:
        exp_id = (
            str(feature.GetField("ID")).strip()
            if feature.GetField("ID")
            else ""
        )
        if exp_id:
            expedientes_en_gpkg.add(exp_id)

        # Caso 1: El registro existe en el Excel y en el GPKG (Actualizar)
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

            # Comprobar si la dirección guardada en el GPKG difiere de la del Excel
            emplaz_antiguo = str(feature.GetField("emplazamiento") or "").strip()
            direccion_modificada = (emplaz_antiguo != emplaz.strip())

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

            # Recalcular coordenadas si no tiene geometría, si la dirección cambió o si se fuerza el recálculo
            if not feature.GetGeometryRef() or direccion_modificada or forzar_recalculo:
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

        # Caso 2: El registro está en el GPKG pero YA NO está en el Excel (Eliminar)
        else:
            if exp_id:
                fids_a_eliminar.append(feature.GetFID())

    # Proceso de borrado explícito de los registros que fueron eliminados del Excel
    for fid in fids_a_eliminar:
        capa.DeleteFeature(fid)

    # Caso 3: Insertar registros nuevos del Excel que no existían en el GPKG
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
        f"GeoPackage actualizado: {registros_actualizados} modificados, "
        f"{registros_nuevos} nuevos, {len(fids_a_eliminar)} eliminados."
    )


def exportar_geopackage_a_kml(ruta_gpkg, ruta_kml):
    """Filtra las ocupaciones vigentes a fecha de hoy y las exporta a formato KML."""
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

    fecha_hoy = datetime.now().strftime("%Y%m%d")
    capa_in.SetAttributeFilter(f"f_fin >= '{fecha_hoy}'")

    if os.path.exists(ruta_kml):
        driver_kml.DeleteDataSource(ruta_kml)

    ds_out = driver_kml.CreateDataSource(ruta_kml)
    ds_out.CopyLayer(capa_in, "ocupaciones_tramos")

    ds_in = None
    ds_out = None
    print(
        f"Éxito: KML exportado con ocupaciones activas a partir de {fecha_hoy}."
    )


def descargar_excel_sharepoint(url_sharepoint):
    """Descarga en memoria el archivo Excel desde SharePoint/OneDrive usando la librería requests."""
    url_limpia = str(url_sharepoint).strip()
    if "download=1" not in url_limpia:
        url_descarga = (
            url_limpia + "&download=1"
            if "?" in url_limpia
            else url_limpia + "?download=1"
        )
    else:
        url_descarga = url_limpia

    print("Descargando libro Excel desde SharePoint con requests...")
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
    }

    response = requests.get(
        url_descarga, headers=headers, timeout=30, allow_redirects=True
    )
    response.raise_for_status()

    return io.BytesIO(response.content)


def excel_sharepoint_to_ics_gpkg(
    origen_excel, rutas_destino, ruta_gpkg, ruta_kml, forzar_recalculo=False
):
    """Pipeline principal de lectura, conversión a calendario ICS, GeoPackage y KML."""
    lineas_ics = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Ayuntamiento Movilidad//Ocupaciones Via Publica//ES",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]

    datos_para_gpkg = {}

    if origen_excel.startswith("http://") or origen_excel.startswith("https://"):
        buffer_excel = descargar_excel_sharepoint(origen_excel)
        df = pd.read_excel(buffer_excel, engine="openpyxl")
    else:
        print(f"Cargando libro Excel local desde: {origen_excel}")
        df = pd.read_excel(origen_excel, engine="openpyxl")

    df = df.fillna("")

    for _, fila in df.iterrows():
        def get_val(col):
            v = fila.get(col, "")
            if pd.isna(v):
                return ""
            return str(v).strip()

        exp_id = get_val("ID")
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

        f_inicio = normalizar_fecha_obj(fila.get("DATA INICI"))
        f_fin = normalizar_fecha_obj(fila.get("DATA FINALITZACIO"))

        if f_inicio and f_fin:
            f_fin_ampliada = f_fin + timedelta(days=1)
            f_inicio_str = f_inicio.strftime("%Y%m%d")
            f_fin_str = f_fin_ampliada.strftime("%Y%m%d")

            lineas_ics.extend([
                "BEGIN:VEVENT",
                f"UID:ocupacion-{exp_id}",
                f"SUMMARY:{emplazamiento}",
                f"DESCRIPTION:{tipo_obra}-{descripcion}-{observaciones}",
                f"DTSTART;VALUE=DATE:{f_inicio_str}",
                f"DTEND;VALUE=DATE:{f_fin_str}",
                "END:VEVENT",
            ])

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

    for ruta in rutas_destino:
        with open(ruta, mode="w", encoding="utf-8") as f_out:
            f_out.write("\n".join(lineas_ics))
            print(f"Éxito: Archivo '{ruta}' generado correctamente.")

    actualizar_geopackage_ogr(ruta_gpkg, datos_para_gpkg, forzar_recalculo=forzar_recalculo)
    exportar_geopackage_a_kml(ruta_gpkg, ruta_kml)


if __name__ == "__main__":
    MODO_PRUEBA = False
    # Cambiar a True para forzar la re-geocodificación de TODAS las direcciones en una ejecución puntual
    FORZAR_RECALCULO_GEO = True

    URL_SHAREPOINT_OFFICIAL = (
        "https://ajtpalma-my.sharepoint.com/:x:/g/personal/pedro_pourtau_palma_es/"
        "IQDgISBCy3jTRJZpXC5jRRflAYITwYojKDvs50WStuiJd90?rtime=YsdeEnAV30g&nav=MTVfezAwMDAwMDAwLTAwMDEtMDAwMC0wMDAwLTAwMDAwMDAwMDAwMH0&download=1"
    )

    if MODO_PRUEBA:
        print("=== INICIANDO EJECUCIÓN EN MODO PRUEBA ===")
        origen_excel = (
            URL_SHAREPOINT_OFFICIAL
            if not os.path.exists("test_ocupaciones.xlsx")
            else "test_ocupaciones.xlsx"
        )
        ruta_ics = "TEST_OCUPACION_VIA_PUBLICA.ics"
        ruta_gpkg = "TEST_OCUPACION_VIA_PUBLICA.gpkg"
        ruta_kml = "TEST_OCUPACION_VIA_PUBLICA.kml"
    else:
        origen_excel = URL_SHAREPOINT_OFFICIAL
        ruta_ics = "OCUPACION_VIA_PUBLICA.ics"
        ruta_gpkg = "OCUPACION_VIA_PUBLICA.gpkg"
        ruta_kml = "OCUPACION_VIA_PUBLICA.kml"

    rutas_destino = [ruta_ics]

    excel_sharepoint_to_ics_gpkg(
        origen_excel,
        rutas_destino,
        ruta_gpkg,
        ruta_kml,
        forzar_recalculo=FORZAR_RECALCULO_GEO,
    )
