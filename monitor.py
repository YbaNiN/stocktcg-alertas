"""Vigila el stock de stocktcg.net y manda notificaciones al móvil (ntfy) cuando
un producto que cumple tus filtros (filtros.toml) vuelve a estar disponible,
baja de precio o se pone por debajo del precio de Cardmarket.
Además genera datos/stock.json, que usa la web (docs/) para mostrar todo lo que hay en stock,
y avisa si el propio monitor deja de funcionar.

Uso:
    python monitor.py            # comprobación normal (la que se hace cada pocos minutos)
    python monitor.py --resumen  # manda un resumen de todo lo que hay en stock ahora mismo
                                 # (no toca el estado de reposiciones)

Variables de entorno:
    NTFY_TOPIC   tema de ntfy al que estás suscrito en el móvil (obligatoria para avisar)
    NTFY_SERVER  servidor ntfy (por defecto https://ntfy.sh)
"""

import gzip
import hashlib
import html as html_lib
import io
import json
import os
import re
import sys
import tomllib
import traceback
import unicodedata
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BASE = "https://stocktcg.net"
STOCK_URL = f"{BASE}/stock.json"
USER_AGENT = "stocktcg-alertas/1.0 (monitor personal; github.com/YbaNiN)"

DIR = Path(__file__).parent
FILTROS = DIR / "filtros.toml"
ESTADO = DIR / "estado" / "estado.json"
DATOS_WEB = DIR / "datos" / "stock.json"
HISTORIAL = DIR / "datos" / "historial.json"
DIAS_HISTORIAL = 120  # días de historial de precios y reposiciones que se guardan
LANZAMIENTOS = DIR / "datos" / "lanzamientos.json"
CALENDARIO_CADA = timedelta(hours=6)  # cada cuánto se vuelve a leer el calendario de StockTCG
DIAS_YA_LANZADO = 14                  # los lanzamientos de hace menos de esto siguen apareciendo
FOTOS = DIR / "datos" / "fotos"  # miniaturas uniformes de las fotos de producto (las usa la web)
TAM_FOTO = 480       # px del cuadrado
MARGEN_FOTO = 0.07   # margen blanco alrededor del producto

# Enlaces en GitHub (GitHub Actions define estas variables)
REPO = os.environ.get("GITHUB_REPOSITORY")
URL_LISTA = (f"https://{REPO.split('/')[0].lower()}.github.io/{REPO.split('/')[1]}/" if REPO
             else f"{BASE}/stock")
URL_ACTIONS = f"https://github.com/{REPO}/actions" if REPO else BASE
# Favoritos: viven en un repositorio aparte (stocktcg-favoritos) que la web puede modificar
DUENO = REPO.split("/")[0] if REPO else "YbaNiN"
URL_FAVORITOS = f"https://raw.githubusercontent.com/{DUENO}/stocktcg-favoritos/main/favoritos.json"

MAX_AVISOS_SUELTOS = 8   # si hay más avisos a la vez, el resto va en un resumen
MAX_LINEAS_RESUMEN = 30  # líneas por notificación de resumen
DATOS_VIEJOS = timedelta(minutes=30)  # si StockTCG no actualiza en este tiempo, se considera un fallo
AVISAR_FALLO_TRAS = 2    # fallos seguidos antes de avisar (evita avisos por un corte puntual)
REPETIR_FALLO_CADA = 36  # mientras siga fallando, recordatorio cada ~3 horas

# Niveles de precio de StockTCG frente a Cardmarket que cuentan como chollo
NIVELES_CHOLLO = {"bueno", "chollo"}  # bueno: 10-44% por debajo · chollo: 15% o más y al mínimo

try:
    MADRID = ZoneInfo("Europe/Madrid")
except ZoneInfoNotFoundError:  # Windows sin el paquete tzdata: se usa la hora local del equipo
    MADRID = datetime.now().astimezone().tzinfo

# Conversión aproximada a euros para aplicar precio_max/precio_min
A_EUROS = {"EUR": 1.0, "DKK": 0.134, "SEK": 0.088, "NOK": 0.086, "CHF": 1.06, "PLN": 0.235}

# Orden en el que se listan los tipos de producto
ORDEN_FORMATOS = ["booster-box", "etb", "booster-bundle", "ultra-premium-collection", "premium-collection",
                  "collection-box", "mini-tin-display", "tin", "mini-tin", "stacking-tin", "blister",
                  "booster-pack", "build-battle", "pin-collection", "deck", "deck-display",
                  "trainers-toolkit", "trainer-collection", "otros"]


# ---------------------------------------------------------------- utilidades

def normalizar(texto):
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", texto.lower()).strip()


def clave(item):
    return hashlib.sha1(item["url"].encode()).hexdigest()[:12]


def precio_euros(item):
    tasa = A_EUROS.get(item.get("currency") or "EUR")
    return item["price"] * tasa if tasa and item.get("price") is not None else None


def formatear_precio(item, precio=None):
    precio = item["price"] if precio is None else precio
    texto = f"{precio:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    moneda = item.get("currency") or "EUR"
    return f"{texto} €" if moneda == "EUR" else f"{texto} {moneda}"


def es_chollo(item):
    return item.get("cmTier") in NIVELES_CHOLLO


def orden_item(item):
    fmt = item.get("fmt")
    pos = ORDEN_FORMATOS.index(fmt) if fmt in ORDEN_FORMATOS else len(ORDEN_FORMATOS)
    return pos, precio_euros(item) or 0


def descargar_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            datos = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                datos = gzip.decompress(datos)
    except Exception as e:
        raise RuntimeError(f"No se puede conectar con StockTCG ({e}).") from e
    try:
        return json.loads(datos)
    except ValueError as e:
        raise RuntimeError("StockTCG ha devuelto datos que no se entienden (¿han cambiado la web?).") from e


def validar_catalogo(datos):
    """Lanza un error si StockTCG ha cambiado su formato o lleva tiempo sin actualizar."""
    items = datos.get("items") if isinstance(datos, dict) else None
    if not items:
        raise RuntimeError("StockTCG ha devuelto un catálogo vacío o con otro formato.")
    faltan = {"url", "store", "title", "setName", "fmt", "price", "available", "langs"} - set(items[0])
    if faltan:
        raise RuntimeError(f"StockTCG ha cambiado el formato de sus datos (faltan: {', '.join(sorted(faltan))}).")
    try:
        ts = datetime.fromisoformat(datos["ts"]).replace(tzinfo=MADRID)
    except (KeyError, ValueError, TypeError):
        return
    if datetime.now(MADRID) - ts > DATOS_VIEJOS:
        raise RuntimeError(f"StockTCG no actualiza sus datos desde el {ts:%d/%m %H:%M}.")


# ---------------------------------------------------------------- filtros

def cargar_filtros():
    try:
        with open(FILTROS, "rb") as f:
            cfg = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise RuntimeError(f"Hay un error al escribir filtros.toml: {e}. Revisa el último cambio que hiciste.") from e
    general = cfg.get("general", {})
    alertas = [a for a in cfg.get("alerta", []) if a.get("activa", True)]
    return general, alertas


def huella_filtros():
    return hashlib.sha1(FILTROS.read_bytes()).hexdigest()[:12]


def coincide(item, alerta, general):
    texto = " " + normalizar(f"{item.get('title')} {item.get('setName')} {item.get('fmtName')} {item.get('fmt')}") + " "

    def tiene(frase):
        # cada palabra de la frase debe aparecer como inicio de palabra ("tin" vale para "tins")
        return all(f" {p}" in texto for p in normalizar(frase).split())

    if alerta.get("palabras") and not any(tiene(f) for f in alerta["palabras"]):
        return False
    if any(tiene(f) for f in alerta.get("excluir", [])):
        return False

    if alerta.get("colecciones"):
        coleccion = normalizar(item.get("setName"))
        if not any(normalizar(c) in coleccion for c in alerta["colecciones"]):
            return False

    if alerta.get("formatos") and item.get("fmt") not in alerta["formatos"]:
        return False

    idiomas = alerta.get("idiomas", general.get("idiomas", []))
    if idiomas:
        del_item = set((item.get("langs") or "").split(","))
        if not del_item & set(idiomas):
            return False

    if alerta.get("tiendas"):
        if normalizar(item.get("store")) not in {normalizar(t) for t in alerta["tiendas"]}:
            return False

    if item.get("preorder") and not alerta.get("preventas", general.get("preventas", True)):
        return False

    euros = precio_euros(item)
    if euros is not None:
        if "precio_max" in alerta and euros > alerta["precio_max"]:
            return False
        if "precio_min" in alerta and euros < alerta["precio_min"]:
            return False

    if "max_sobreprecio" in alerta and item.get("cmPctTrend") is not None:
        if item["cmPctTrend"] > alerta["max_sobreprecio"]:
            return False

    return True


def primer_filtro(item, alertas, general):
    excluidas = {normalizar(t) for t in general.get("tiendas_excluidas", [])}
    if normalizar(item.get("store")) in excluidas:
        return None
    for alerta in alertas:
        if coincide(item, alerta, general):
            return alerta
    return None


def agrupar(coinciden, alertas):
    """{nombre del filtro: [items ordenados]} manteniendo el orden de filtros.toml"""
    grupos = {a.get("nombre", "sin nombre"): [] for a in alertas}
    for item, alerta in coinciden:
        grupos[alerta.get("nombre", "sin nombre")].append(item)
    for lista in grupos.values():
        lista.sort(key=orden_item)
    return grupos


# ---------------------------------------------------------------- notificaciones

def enviar_ntfy(mensaje):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("  (sin NTFY_TOPIC: no se envía)", mensaje.get("title"))
        return
    servidor = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    cuerpo = json.dumps({"topic": topic, **mensaje}).encode()
    req = urllib.request.Request(servidor, data=cuerpo, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": USER_AGENT})
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:  # una notificación fallida no debe parar el resto
        print("  ERROR enviando a ntfy:", e)


def url_coleccion(nombre):
    return f"{URL_LISTA}#{urllib.parse.quote(nombre)}" if nombre else URL_LISTA


def linea_cardmarket(item):
    if not item.get("cmTrend"):
        return None
    pct = item.get("cmPctTrend")
    return f"Cardmarket: {item['cmTrend']:.2f} €" + (f" ({pct:+.0f}%)" if pct is not None else "")


def grupo_de(item, alerta):
    """Identificador de producto que usa la web: filtro + página de StockTCG (o la url si no tiene)."""
    return f"{alerta.get('nombre', 'sin nombre')}|{BASE + item['ppage'] if item.get('ppage') else item['url']}"


def cargar_preferencias():
    """Favoritos y tiendas ocultas que el usuario marca en la web (repositorio stocktcg-favoritos)."""
    try:
        req = urllib.request.Request(f"{URL_FAVORITOS}?t={int(datetime.now().timestamp() // 60)}",
                                     headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            datos = json.loads(resp.read())
        return set(datos.get("favoritos", [])), {normalizar(t) for t in datos.get("tiendas_ocultas", [])}
    except Exception as e:  # sin preferencias no pasa nada: se avisa igual, sin prioridad
        print("  (no se han podido leer los favoritos:", e, ")")
        return set(), set()


def aviso_producto(tipo, item, alerta, precio_antes=None, favorito=False):
    """tipo: 'reposicion', 'bajada', 'chollo' o 'filtro'"""
    producto = f"{item.get('setName')} · {item.get('fmtName')}"
    chollo = " 🔥" if es_chollo(item) else ""
    lugar = f"en {item.get('store')} · {(item.get('langs') or '?').upper()}"

    if tipo == "reposicion":
        titulo = f"{'🕒 Preventa' if item.get('preorder') else '🟢 En stock'}{chollo}: {producto}"
        primera = f"{formatear_precio(item)} {lugar}"
        etiqueta = "hourglass" if item.get("preorder") else "rotating_light"
    elif tipo == "bajada":
        bajada = (1 - item["price"] / precio_antes) * 100
        titulo = f"📉 Baja de precio{chollo}: {producto}"
        primera = f"{formatear_precio(item)} (antes {formatear_precio(item, precio_antes)}, -{bajada:.0f}%) {lugar}"
        etiqueta = "chart_with_downwards_trend"
    elif tipo == "chollo":
        titulo = f"🔥 Chollo: {producto}"
        primera = f"{formatear_precio(item)} {lugar} · por debajo de Cardmarket"
        etiqueta = "fire"
    elif tipo == "preventa_es":
        producto = f"{nombre_es(item.get('setName'))} · {item.get('fmtName')}"
        titulo = f"🗓️ Nueva preventa en español: {producto}"
        lanz = item.get("_lanzamiento")
        sale = f" · sale el {int(lanz['fecha'][8:])}/{int(lanz['fecha'][5:7])}" if lanz else ""
        primera = f"{formatear_precio(item)} en {item.get('store')}{sale}"
        etiqueta = "date"
    else:  # 'filtro': ya estaba en stock pero ahora cumple tu filtro (p. ej. ha bajado de tu precio máximo)
        titulo = f"🎯 Ahora cumple tu filtro: {producto}"
        primera = f"{formatear_precio(item)} {lugar}"
        etiqueta = "dart"

    lineas = [primera, item.get("title") or "", linea_cardmarket(item),
              "Fuera de tus filtros: te aviso porque es una preventa en español" if tipo == "preventa_es"
              else f"Filtro: {alerta.get('nombre', 'sin nombre')}"]
    mensaje = {
        "title": ("❤️ " if favorito else "") + titulo,
        "message": "\n".join(l for l in lineas if l),
        "click": item["url"],
        "priority": 5 if favorito else 4,  # 5 = urgente: ntfy la hace sonar con más insistencia
        "tags": [etiqueta],
        "actions": [{"action": "view", "label": "Reservar" if tipo == "preventa_es" else "Comprar", "url": item["url"]}],
    }
    if item.get("ppage"):
        mensaje["actions"].append({"action": "view", "label": "StockTCG", "url": BASE + item["ppage"]})
    mensaje["actions"].append({"action": "view", "label": "Ver en la app",
                               "url": f"{URL_LISTA}#lanzamientos" if tipo == "preventa_es" else url_coleccion(alerta.get("nombre", ""))})
    if item.get("img"):
        mensaje["attach"] = item["img"]
    return mensaje


def linea_corta(item):
    extra = (" (preventa)" if item.get("preorder") else "") + (" 🔥" if es_chollo(item) else "")
    return f"• {item.get('fmtName')}{extra} · {formatear_precio(item)} · {item.get('store')}"


def enviar_resumen(grupos, motivo):
    for nombre, items in grupos.items():
        lineas = [linea_corta(i) for i in items[:MAX_LINEAS_RESUMEN]]
        if len(items) > MAX_LINEAS_RESUMEN:
            lineas.append(f"… y {len(items) - MAX_LINEAS_RESUMEN} más (toca para verlos todos)")
        enviar_ntfy({
            "title": f"📦 {nombre}: {len(items)} en stock",
            "message": "\n".join(lineas) if lineas else "Ahora mismo no hay nada en stock con este filtro.",
            "click": url_coleccion(nombre),
            "tags": ["package"],
            "actions": [{"action": "view", "label": "Abrir la app", "url": url_coleccion(nombre)}],
        })
    print(f"Resumen enviado ({motivo}).")


def enviar_eventos(eventos, favoritos=frozenset()):
    es_fav = lambda ev: grupo_de(ev[1], ev[2]) in favoritos
    eventos = sorted(eventos, key=lambda ev: not es_fav(ev))  # favoritos primero
    for ev in eventos[:MAX_AVISOS_SUELTOS]:
        tipo, item, alerta, precio_antes = ev
        print(f"  -> [{tipo}{' ❤' if es_fav(ev) else ''}] {item.get('setName')} · {item.get('fmtName')} · {formatear_precio(item)} · {item.get('store')}")
        enviar_ntfy(aviso_producto(tipo, item, alerta, precio_antes, es_fav(ev)))

    resto = eventos[MAX_AVISOS_SUELTOS:]
    if resto:
        iconos = {"reposicion": "🟢", "bajada": "📉", "chollo": "🔥", "filtro": "🎯", "preventa_es": "🗓️"}
        lineas = [f"{iconos[t]} {i.get('setName')} · {i.get('fmtName')} · {formatear_precio(i)} · {i.get('store')}"
                  for t, i, _, _ in resto[:15]]
        if len(resto) > 15:
            lineas.append(f"… y {len(resto) - 15} más")
        enviar_ntfy({"title": f"🟢 {len(resto)} avisos más", "message": "\n".join(lineas),
                     "click": URL_LISTA, "priority": 4, "tags": ["rotating_light"]})


# ---------------------------------------------------------------- lanzamientos y preventas en español

def es_espanol(item):
    return "es" in (item.get("langs") or "").split(",")


def nombre_es(nombre):
    """'Delta Reign / Dominio Delta (Mega Evolution)' -> 'Dominio Delta'"""
    if " / " in (nombre or ""):
        return re.sub(r"\s*\([^)]*\)\s*$", "", nombre.split(" / ")[-1]).strip()
    return nombre or ""


def descargar_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=40) as resp:
        datos = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            datos = gzip.decompress(datos)
    return datos.decode("utf-8", "replace")


def texto_html(fragmento):
    return html_lib.unescape(re.sub(r"<[^>]+>", "", fragmento or "")).strip()


def leer_calendario():
    """Lee el calendario de https://stocktcg.net/lanzamientos y, de cada lanzamiento, los enlaces exactos
    de los productos de las tiendas (así cada oleada se queda solo con sus productos)."""
    pagina = descargar_html(f"{BASE}/lanzamientos")
    lanzamientos = []
    for bloque in re.findall(r'<article class="release[^"]*">(.*?)</article>', pagina, re.S):
        fecha = re.search(r'<time[^>]*datetime="(\d{4}-\d{2}-\d{2})"', bloque)
        enlace = re.search(r'<a class="rel-link" href="/lanzamientos/([^"]+)">(.*?)</a>', bloque, re.S)
        if not (fecha and enlace):
            continue
        tipo = re.search(r'<p class="rel-type">(.*?)</p>', bloque, re.S)
        img = re.search(r'<div class="rel-img"><img src="([^"]+)"', bloque)
        lanzamientos.append({
            "slug": enlace.group(1), "nombre": texto_html(enlace.group(2)), "tipo": texto_html(tipo.group(1) if tipo else ""),
            "fecha": fecha.group(1), "fechaConfirmada": "por confirmar" not in bloque.lower(),
            "img": html_lib.unescape(img.group(1)) if img else None,
        })
    if not lanzamientos:
        raise RuntimeError("no se ha encontrado ningún lanzamiento en la página (¿ha cambiado la web?)")
    limite = (datetime.now(MADRID) - timedelta(days=DIAS_YA_LANZADO)).date().isoformat()
    lanzamientos = [l for l in lanzamientos if l["fecha"] >= limite]
    for l in lanzamientos:
        try:
            ficha = descargar_html(f"{BASE}/lanzamientos/{l['slug']}")
            cuerpo = ficha[ficha.find("<main"):ficha.find("</main>")]
            l["urls"] = sorted({html_lib.unescape(u) for u in re.findall(r'href="(https?://[^"]+)"', cuerpo)
                                if "stocktcg.net" not in u and "t.me/" not in u})
        except Exception as e:
            print(f"  (no se ha podido leer la ficha de {l['slug']}: {e})")
            l["urls"] = []
    return lanzamientos


def obtener_calendario(anterior):
    """Calendario guardado en el estado; se renueva cada CALENDARIO_CADA."""
    guardado = (anterior or {}).get("calendario") or {}
    try:
        viejo = datetime.now(MADRID) - datetime.fromisoformat(guardado["ts"]) > CALENDARIO_CADA
    except (KeyError, ValueError, TypeError):
        viejo = True
    if not viejo:
        return guardado
    try:
        lanzamientos = leer_calendario()
        print(f"Calendario de lanzamientos actualizado: {len(lanzamientos)} lanzamientos.")
        return {"ts": datetime.now(MADRID).isoformat(timespec="minutes"), "lanzamientos": lanzamientos}
    except Exception as e:  # sin calendario el monitor sigue funcionando
        print(f"  (no se ha podido leer el calendario de lanzamientos: {e})")
        return guardado or {"ts": None, "lanzamientos": []}


def oferta_web(item, fotos):
    return {
        "titulo": item.get("title"), "coleccion": item.get("setName"), "tipo": item.get("fmt"),
        "tipoNombre": item.get("fmtName"), "precio": item.get("price"), "moneda": item.get("currency") or "EUR",
        "tienda": item.get("store"), "idioma": item.get("langs"), "url": item["url"], "img": item.get("img"),
        "foto": (fotos or {}).get(item.get("img")), "preventa": bool(item.get("preorder")),
        "stocktcg": BASE + item["ppage"] if item.get("ppage") else None,
    }


def cruzar_lanzamientos(calendario, disponibles):
    """Une el calendario con el catálogo. Devuelve los lanzamientos con sus ofertas en español
    y todas las preventas en español (de cualquier colección)."""
    por_url = {i["url"]: i for i in disponibles if es_espanol(i)}
    lanzamientos, fecha_de = [], {}
    for l in sorted(calendario.get("lanzamientos", []), key=lambda x: x["fecha"]):
        ofertas = sorted((por_url[u] for u in l.get("urls", []) if u in por_url), key=lambda i: i["price"])
        for i in ofertas:
            fecha_de.setdefault(i["url"], l)
        lanzamientos.append({**{k: v for k, v in l.items() if k != "urls"}, "items": ofertas})
    preventas = sorted((i for i in disponibles if i.get("preorder") and es_espanol(i)),
                       key=lambda i: (nombre_es(i.get("setName")), i.get("fmt") or "", i["price"]))
    for i in preventas:
        if i["url"] in fecha_de:
            i["_lanzamiento"] = fecha_de[i["url"]]
    return lanzamientos, preventas


def escribir_lanzamientos(lanzamientos, preventas, fotos, ts):
    contenido = {
        "lanzamientos": [{**{k: v for k, v in l.items() if k != "items"},
                          "foto": (fotos or {}).get(l.get("img")),
                          "ofertas": [oferta_web(i, fotos) for i in l["items"]]} for l in lanzamientos],
        "preventas": [{**oferta_web(i, fotos), "lanzamiento": i.get("_lanzamiento", {}).get("slug"),
                       "fecha": i.get("_lanzamiento", {}).get("fecha")} for i in preventas],
    }
    if LANZAMIENTOS.exists():
        try:
            viejo = json.loads(LANZAMIENTOS.read_text(encoding="utf-8"))
            viejo.pop("actualizado", None)
            if viejo == contenido:
                return False
        except ValueError:
            pass
    LANZAMIENTOS.parent.mkdir(exist_ok=True)
    LANZAMIENTOS.write_text(json.dumps({"actualizado": ts, **contenido}, ensure_ascii=False, indent=1), encoding="utf-8")
    return True


# ---------------------------------------------------------------- fotos

def generar_miniatura(url, ruta):
    """Descarga la foto de la tienda y la deja como un cuadrado blanco con el producto centrado.
    Si el fondo es claro (blanco, blanco roto, gris muy claro) se recorta el margen sobrante y el
    fondo se pasa a blanco puro; si es una foto con fondo de color se deja entera."""
    from PIL import Image, ImageChops, ImageFilter, ImageOps
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; stocktcg-alertas)"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        im = Image.open(io.BytesIO(resp.read()))
        im.load()
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        fondo = Image.new("RGBA", im.size, (255, 255, 255, 255))
        fondo.alpha_composite(im)
        im = fondo
    im = im.convert("RGB")

    w, h = im.size
    puntos = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]
    borde = [im.getpixel(pt) for pt in puntos]
    claro = sum(min(c) >= 222 and max(c) - min(c) <= 20 for c in borde) >= 6  # casi todo el borde es claro y neutro
    if claro:
        color = tuple(sorted(c[i] for c in borde)[len(borde) // 2] for i in range(3))  # color de fondo (mediana)
        distinto = ImageChops.difference(im, Image.new("RGB", im.size, color)).convert("L")
        contenido = distinto.point(lambda v: 255 if v > 24 else 0).filter(ImageFilter.MedianFilter(3))
        caja = contenido.getbbox()
        if caja:
            # El fondo pasa a blanco puro (con borde suave para no dejar halo) y se recorta lo que sobra
            mascara = contenido.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(1.2))
            im = Image.composite(im, Image.new("RGB", im.size, (255, 255, 255)), mascara).crop(caja)

    area = round(TAM_FOTO * (1 - 2 * MARGEN_FOTO))
    escala = min(area / im.width, area / im.height)
    im = im.resize((max(1, round(im.width * escala)), max(1, round(im.height * escala))), Image.LANCZOS)
    lienzo = Image.new("RGB", (TAM_FOTO, TAM_FOTO), (255, 255, 255))
    lienzo.paste(im, ((TAM_FOTO - im.width) // 2, (TAM_FOTO - im.height) // 2))
    ruta.parent.mkdir(parents=True, exist_ok=True)
    lienzo.save(ruta, "JPEG", quality=78, optimize=True, progressive=True)


def preparar_fotos(urls):
    """Genera las miniaturas que falten y borra las que ya no se usan. Devuelve {url original: ruta en datos/}."""
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("  (sin Pillow: la web usará las fotos originales)")
        return {}
    mapa, nuevas = {}, 0
    for url in sorted(urls):
        nombre = hashlib.sha1(url.encode()).hexdigest()[:16] + ".jpg"
        ruta = FOTOS / nombre
        if not ruta.exists():
            try:
                generar_miniatura(url, ruta)
                nuevas += 1
            except Exception as e:  # una foto que falla no debe parar el monitor
                print(f"  (foto no preparada: {url[:70]} · {e})")
                continue
        mapa[url] = f"fotos/{nombre}"
    usadas = set(mapa.values())
    for f in FOTOS.glob("*.jpg"):
        if f"fotos/{f.name}" not in usadas:
            f.unlink()
    if nuevas:
        print(f"Fotos preparadas: {nuevas} nuevas ({len(mapa)} en total).")
    return mapa


# ---------------------------------------------------------------- datos para la web

def escribir_datos_web(grupos, info, ts_catalogo, fotos=None):
    """Escribe datos/stock.json para la web. Solo lo reescribe si algo ha cambiado (así no hay commits de más)."""
    colecciones = []
    for nombre, items in grupos.items():
        lista = []
        for i in items:
            desde, precio_antes, ts_bajada = info.get(clave(i), (None, None, None))
            lista.append({
                "titulo": i.get("title"), "coleccion": i.get("setName"), "tipo": i.get("fmt"),
                "tipoNombre": i.get("fmtName"), "precio": i.get("price"), "moneda": i.get("currency") or "EUR",
                "tienda": i.get("store"), "idioma": i.get("langs"), "url": i["url"], "img": i.get("img"),
                "foto": (fotos or {}).get(i.get("img")),
                "preventa": bool(i.get("preorder")), "cardmarket": i.get("cmTrend"),
                "cardmarketPct": i.get("cmPctTrend"), "chollo": es_chollo(i),
                "stocktcg": BASE + i["ppage"] if i.get("ppage") else None,
                "desde": desde, "precioAntes": precio_antes, "bajadaDesde": ts_bajada,
            })
        colecciones.append({"nombre": nombre, "productos": lista})
    contenido = {"colecciones": colecciones}

    if DATOS_WEB.exists():
        try:
            viejo = json.loads(DATOS_WEB.read_text(encoding="utf-8"))
            viejo.pop("actualizado", None)
            if viejo == contenido:
                return False
        except ValueError:
            pass
    DATOS_WEB.parent.mkdir(exist_ok=True)
    DATOS_WEB.write_text(json.dumps({"actualizado": ts_catalogo, **contenido}, ensure_ascii=False, indent=1),
                         encoding="utf-8")
    return True


def actualizar_historial(coinciden, eventos, disponibles, obsoletas, ahora):
    """Guarda en datos/historial.json el precio mínimo de cada día por producto y cada reposición
    (cuándo entró y cuándo se agotó). Solo reescribe el archivo si algo ha cambiado."""
    try:
        hist = json.loads(HISTORIAL.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        hist = {}
    precios = hist.setdefault("precios", {})
    reposiciones = hist.setdefault("reposiciones", [])
    antes = json.dumps(hist, sort_keys=True)
    hoy = ahora[:10]

    for item, alerta in coinciden:
        euros = precio_euros(item)
        if euros is None:
            continue
        dias = precios.setdefault(grupo_de(item, alerta), {})
        dias[hoy] = round(min(euros, dias.get(hoy, euros)), 2)

    abiertas = {r["id"] for r in reposiciones if not r.get("salida")}
    for tipo, item, alerta, _ in eventos:
        if tipo == "reposicion" and clave(item) not in abiertas:
            reposiciones.append({"id": clave(item), "grupo": grupo_de(item, alerta), "filtro": alerta.get("nombre"),
                                 "tienda": item["store"], "precio": item["price"], "titulo": item.get("title"),
                                 "entrada": ahora, "salida": None})

    # Agotados: reposiciones abiertas cuyo producto ya no está disponible (si la tienda tiene datos al día)
    en_stock = {clave(i) for i in disponibles}
    for r in reposiciones:
        if not r.get("salida") and r["id"] not in en_stock and r["tienda"] not in obsoletas:
            r["salida"] = ahora

    # Limpiar lo más antiguo
    limite = (datetime.now(MADRID) - timedelta(days=DIAS_HISTORIAL)).isoformat()[:10]
    for k in list(precios):
        precios[k] = {d: v for d, v in precios[k].items() if d >= limite}
        if not precios[k]:
            del precios[k]
    hist["reposiciones"] = [r for r in reposiciones if r["entrada"][:10] >= limite]

    if json.dumps(hist, sort_keys=True) == antes:
        return False
    HISTORIAL.parent.mkdir(exist_ok=True)
    HISTORIAL.write_text(json.dumps(hist, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return True


# ---------------------------------------------------------------- principal

def comprobar(solo_resumen, anterior):
    """Hace una comprobación completa. Devuelve el nuevo estado a guardar (o None si no hay que guardarlo)."""
    general, alertas = cargar_filtros()
    print(f"Filtros activos: {len(alertas)}")

    datos = descargar_json(STOCK_URL)
    validar_catalogo(datos)
    items = datos["items"]
    obsoletas = set(datos.get("stale_stores", []))
    print(f"Catálogo de {datos.get('ts')}: {len(items)} productos ({len(obsoletas)} tiendas sin actualizar)")

    disponibles = [i for i in items if i.get("available") and i.get("url")]
    coinciden = [(i, a) for i in disponibles if (a := primer_filtro(i, alertas, general))]
    grupos = agrupar(coinciden, alertas)
    for nombre, lista in grupos.items():
        print(f"  {nombre}: {len(lista)} en stock")

    huella = huella_filtros()
    misma_config = anterior is not None and anterior.get("filtros") == huella
    antes = {k for claves in anterior["tiendas"].values() for k in claves} if anterior else set()
    seguidos_antes = (anterior or {}).get("seguidos")  # None si el estado es de una versión antigua
    ahora = datetime.now(MADRID).isoformat(timespec="minutes")

    avisar_bajadas = general.get("avisar_bajadas", True)
    bajada_minima = general.get("bajada_minima", 5)
    avisar_chollos = general.get("avisar_chollos", True)

    # Para cada producto que cumple tus filtros: qué ha pasado y desde cuándo
    eventos, seguidos, info = [], {}, {}
    for item, alerta in coinciden:
        k = clave(item)
        previo = (seguidos_antes or {}).get(k)
        previo = (previo or []) + [None] * (6 - len(previo or []))  # entradas antiguas tenían 3 campos
        desde, precio_antes, ts_bajada = previo[3], previo[4], previo[5]

        if misma_config and item["store"] not in obsoletas:
            if k not in antes:
                eventos.append(("reposicion", item, alerta, None))
                desde, precio_antes, ts_bajada = ahora, None, None
            elif seguidos_antes is not None and previo[0] is None:
                eventos.append(("filtro", item, alerta, None))
                desde = ahora
            elif seguidos_antes is not None:
                if avisar_bajadas and previo[0] and item["price"] < previo[0] * (1 - bajada_minima / 100):
                    eventos.append(("bajada", item, alerta, previo[0]))
                    precio_antes, ts_bajada = previo[0], ahora
                elif avisar_chollos and es_chollo(item) and previo[1] not in NIVELES_CHOLLO:
                    eventos.append(("chollo", item, alerta, None))

        if precio_antes is not None and item["price"] >= precio_antes:
            precio_antes, ts_bajada = None, None  # ha vuelto a subir: ya no es una bajada
        seguidos[k] = [item["price"], item.get("cmTier"), item["store"], desde, precio_antes, ts_bajada]
        info[k] = (desde, precio_antes, ts_bajada)

    calendario = obtener_calendario(anterior)
    lanzamientos, preventas_es = cruzar_lanzamientos(calendario, disponibles)

    imagenes = {i["img"] for i, _ in coinciden if i.get("img")}
    imagenes |= {i["img"] for i in preventas_es if i.get("img")}
    imagenes |= {i["img"] for l in lanzamientos for i in l["items"] if i.get("img")}
    imagenes |= {l["img"] for l in lanzamientos if l.get("img")}
    fotos = preparar_fotos(imagenes)
    if escribir_datos_web(grupos, info, datos.get("ts"), fotos):
        print("datos/stock.json actualizado.")
    if escribir_lanzamientos(lanzamientos, preventas_es, fotos, datos.get("ts")):
        print("datos/lanzamientos.json actualizado.")

    if solo_resumen:
        enviar_resumen(grupos, "pedido a mano")
        return None

    # Productos disponibles por tienda
    tiendas = {}
    for item in disponibles:
        tiendas.setdefault(item["store"], []).append(clave(item))
    if anterior:
        # Las tiendas sin datos recientes conservan su estado anterior para no dar falsos avisos
        for tienda in obsoletas:
            if tienda in anterior["tiendas"]:
                tiendas[tienda] = anterior["tiendas"][tienda]
        for k, v in (seguidos_antes or {}).items():
            if v[2] in obsoletas:
                seguidos.setdefault(k, v)

    # Preventas en español nuevas (de cualquier colección): se avisan aunque no estén en tus filtros.
    # Las que ya cumplen un filtro no se repiten: ya llegan como reposición/preventa normal.
    vistas_antes = set((anterior or {}).get("preventas_es") or [])
    if anterior and "preventas_es" in anterior and general.get("avisar_preventas_es", True):
        en_filtros = {clave(i) for i, _ in coinciden}
        for i in preventas_es:
            k = clave(i)
            if k not in vistas_antes and k not in en_filtros and i["store"] not in obsoletas:
                eventos.append(("preventa_es", i, {"nombre": "Preventas en español"}, None))

    nuevo_estado = {"ts": datos.get("ts"), "filtros": huella, "tiendas": tiendas, "seguidos": seguidos, "errores": 0,
                    "preventas_es": sorted({clave(i) for i in preventas_es}), "calendario": calendario}

    if actualizar_historial(coinciden, eventos if misma_config else [], disponibles, obsoletas, ahora):
        print("datos/historial.json actualizado.")

    if not misma_config:
        # Primera vez o filtros cambiados: resumen de lo que ya hay (y no se avisa como reposición)
        enviar_resumen(grupos, "primera ejecución" if anterior is None else "filtros cambiados")
        return nuevo_estado

    favoritos, ocultas = cargar_preferencias() if eventos else (set(), set())
    if ocultas:
        eventos = [ev for ev in eventos if normalizar(ev[1]["store"]) not in ocultas]
    print(f"Avisos: {len(eventos)}")
    enviar_eventos(eventos, favoritos)
    return nuevo_estado


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    solo_resumen = "--resumen" in sys.argv

    guardado = json.loads(ESTADO.read_text()) if ESTADO.exists() else {}
    anterior = guardado if "tiendas" in guardado else None
    errores_antes = guardado.get("errores", 0)

    try:
        nuevo = comprobar(solo_resumen, anterior)
    except Exception as e:
        traceback.print_exc()
        errores = errores_antes + 1
        print(f"::error::El monitor ha fallado ({errores} veces seguidas): {e}")
        if not solo_resumen:
            ESTADO.parent.mkdir(exist_ok=True)
            ESTADO.write_text(json.dumps({**guardado, "errores": errores}))
        if solo_resumen or errores == AVISAR_FALLO_TRAS or (errores > AVISAR_FALLO_TRAS and errores % REPETIR_FALLO_CADA == 0):
            enviar_ntfy({
                "title": "⚠️ El monitor de stock está fallando",
                "message": f"{e}\n\nMientras siga así no te llegarán avisos de stock. "
                           f"Te avisaré cuando vuelva a funcionar.",
                "click": URL_ACTIONS, "priority": 4, "tags": ["warning"],
            })
        return

    if nuevo is not None:
        if errores_antes >= AVISAR_FALLO_TRAS:
            enviar_ntfy({"title": "✅ El monitor de stock vuelve a funcionar",
                         "message": f"Ha estado fallando unos {errores_antes * 5} minutos. Ya vuelves a recibir avisos.",
                         "click": URL_LISTA, "tags": ["white_check_mark"]})
        ESTADO.parent.mkdir(exist_ok=True)
        ESTADO.write_text(json.dumps(nuevo))


if __name__ == "__main__":
    main()
