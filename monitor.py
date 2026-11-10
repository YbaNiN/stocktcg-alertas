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
from collections import Counter
from datetime import date, datetime, timedelta
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
MINUTOS_RELAMPAGO = 10  # una reposición que se agota en esto o menos cuenta como "relámpago"
LANZAMIENTOS = DIR / "datos" / "lanzamientos.json"
AMAZON = DIR / "datos" / "amazon.json"
AMAZON_CADA = timedelta(minutes=30)  # cada cuánto se vuelve a leer la página de invitaciones de Amazon
CALENDARIO_CADA = timedelta(hours=6)  # cada cuánto se vuelve a leer el calendario de StockTCG
DIAS_YA_LANZADO = 14                  # los lanzamientos de hace menos de esto siguen apareciendo
FOTOS = DIR / "datos" / "fotos"  # miniaturas uniformes de las fotos de producto (las usa la web)
TAM_FOTO = 480       # px del cuadrado
MARGEN_FOTO = 0.07   # margen blanco alrededor del producto
MAX_FOTOS_NUEVAS = 40  # por comprobación, para no pasarse de tiempo; las demás se preparan en las siguientes

# Enlaces en GitHub (GitHub Actions define estas variables)
REPO = os.environ.get("GITHUB_REPOSITORY")
URL_LISTA = (f"https://{REPO.split('/')[0].lower()}.github.io/{REPO.split('/')[1]}/" if REPO
             else f"{BASE}/stock")
URL_ACTIONS = f"https://github.com/{REPO}/actions" if REPO else BASE
# Favoritos: viven en un repositorio aparte (stocktcg-favoritos) que la web puede modificar
DUENO = REPO.split("/")[0] if REPO else "YbaNiN"
URL_FAVORITOS = f"https://raw.githubusercontent.com/{DUENO}/stocktcg-favoritos/main/favoritos.json"
# Los filtros de avisos se editan desde la app y se guardan en el mismo repositorio (filtros.toml queda de respaldo)
URL_FILTROS_APP = f"https://raw.githubusercontent.com/{DUENO}/stocktcg-favoritos/main/filtros.json"

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


class CatalogoAtascado(RuntimeError):
    """El catálogo de StockTCG tiene el formato correcto pero lleva tiempo sin actualizarse."""


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
        raise CatalogoAtascado(f"StockTCG no actualiza sus datos desde el {ts:%d/%m %H:%M}.")


# ---------------------------------------------------------------- filtros

def limpiar_alerta(a):
    """Valida un filtro que viene de la app: solo campos conocidos y con el tipo correcto."""
    if not isinstance(a, dict) or not str(a.get("nombre", "")).strip():
        return None
    listas = ("palabras", "excluir", "colecciones", "formatos", "idiomas", "tiendas")
    limpio = {"nombre": str(a["nombre"]).strip()[:60], "activa": bool(a.get("activa", True))}
    for k in listas:
        if isinstance(a.get(k), list):
            limpio[k] = [str(x).strip() for x in a[k] if str(x).strip()][:30]
    for k in ("precio_max", "precio_min", "max_sobreprecio"):
        if isinstance(a.get(k), (int, float)) and not isinstance(a.get(k), bool) and a[k] >= 0:
            limpio[k] = a[k]
    if isinstance(a.get("preventas"), bool):
        limpio["preventas"] = a["preventas"]
    if isinstance(a.get("precio_min_tipo"), dict):  # p. ej. {"booster-box": 120}: precio mínimo solo para ese tipo
        limpio["precio_min_tipo"] = {str(k): v for k, v in a["precio_min_tipo"].items()
                                     if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0}
    return limpio


def filtros_de_la_app():
    """Filtros guardados desde la web (stocktcg-favoritos/filtros.json). None si no hay o no se pueden leer."""
    try:
        req = urllib.request.Request(f"{URL_FILTROS_APP}?t={int(datetime.now().timestamp() // 60)}",
                                     headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=20) as resp:
            datos = json.loads(resp.read())
        alertas = [x for x in (limpiar_alerta(a) for a in datos.get("alertas", [])) if x]
        return alertas or None
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    except Exception as e:
        print("  (no se han podido leer los filtros de la app:", e, ")")
        raise


def cargar_filtros(anterior=None):
    """Devuelve (general, alertas activas, todas las alertas, alertas de la app).
    Manda lo que se edita en la app; si nunca se ha usado, filtros.toml. Si la app no responde,
    se usan los últimos filtros de la app que se leyeron bien (guardados en el estado)."""
    try:
        with open(FILTROS, "rb") as f:
            cfg = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise RuntimeError(f"Hay un error al escribir filtros.toml: {e}. Revisa el último cambio que hiciste.") from e
    general = cfg.get("general", {})
    try:
        de_app = filtros_de_la_app()
    except Exception:
        de_app = (anterior or {}).get("alertas_app")
    todas = de_app or cfg.get("alerta", [])
    return general, [a for a in todas if a.get("activa", True)], todas, de_app


NO_SON_FILTROS = {"caducidad_clave_cronjob", "avisar_amazon"}


def huella_filtros(general, todas):
    general = {k: v for k, v in general.items() if k not in NO_SON_FILTROS}
    return hashlib.sha1(json.dumps({"general": general, "alertas": todas}, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()[:12]


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
        minimo_tipo = (alerta.get("precio_min_tipo") or {}).get(item.get("fmt"))
        if minimo_tipo is not None and euros < minimo_tipo:
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


# Tipos en los que una página de StockTCG es siempre el mismo producto (sin modelos distintos)
UN_SOLO_MODELO = {"booster-box", "etb", "booster-bundle", "ultra-premium-collection", "mini-tin-display",
                  "deck-display", "trainers-toolkit"}


def unificar_fichas(coinciden):
    """StockTCG a veces reparte un mismo producto en varias páginas (la UPC de Mega Charizard X sale en
    "Phantasmal Flames" y en "Mega Evolution") o lo deja sin página. Dentro de cada filtro, los productos
    de un tipo con un solo modelo se juntan en una ficha: la de la colección principal del filtro."""
    por_filtro = {}
    for i, a in coinciden:
        por_filtro.setdefault(a.get("nombre"), []).append(i)
    principal = {n: Counter(i.get("setName") for i in l).most_common(1)[0][0] for n, l in por_filtro.items()}
    paginas = {}  # (filtro, tipo) -> Counter de páginas
    for i, a in coinciden:
        if i.get("fmt") in UN_SOLO_MODELO and i.get("ppage"):
            paginas.setdefault((a.get("nombre"), i["fmt"]), Counter())[(i["ppage"], i.get("setName"))] += 1
    base = {k: max(c.items(), key=lambda pc: (pc[0][1] == principal[k[0]], pc[1]))[0] for k, c in paginas.items()}
    unificados = []
    for i, a in coinciden:
        b = base.get((a.get("nombre"), i.get("fmt")))
        if b and i.get("ppage") != b[0]:
            i = {**i, "ppage": b[0], "setName": b[1]}
        unificados.append((i, a))
    return unificados


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


def tiendas_relampago():
    """Tiendas cuyo stock se agota casi siempre en minutos (según el historial de reposiciones)."""
    try:
        hist = json.loads(HISTORIAL.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return set()
    stats = {}
    for r in hist.get("reposiciones", []):
        if not r.get("salida"):
            continue
        minutos = (datetime.fromisoformat(r["salida"]) - datetime.fromisoformat(r["entrada"])).total_seconds() / 60
        n, rapidas = stats.get(r["tienda"], (0, 0))
        stats[r["tienda"]] = (n + 1, rapidas + (minutos <= MINUTOS_RELAMPAGO))
    return {t for t, (n, rapidas) in stats.items() if n >= 3 and rapidas / n >= 0.6}


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


def aviso_producto(tipo, item, alerta, precio_antes=None, favorito=False, relampago=False):
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
    elif tipo == "amazon":
        producto = f"{nombre_es(item.get('setName'))} · {item.get('fmtName')}"
        titulo = f"📨 Invitación de Amazon abierta: {producto}"
        primera = f"{formatear_precio(item)} en Amazon (precio oficial) · pide la invitación antes de que cierre"
        etiqueta = "envelope"
    else:  # 'filtro': ya estaba en stock pero ahora cumple tu filtro (p. ej. ha bajado de tu precio máximo)
        titulo = f"🎯 Ahora cumple tu filtro: {producto}"
        primera = f"{formatear_precio(item)} {lugar}"
        etiqueta = "dart"

    lineas = [primera, item.get("title") or "", linea_cardmarket(item),
              "⚡ En esta tienda suele agotarse en minutos: date prisa" if relampago else "",
              "Pedirla es gratis y no te compromete: Amazon reparte las invitaciones por email días después." if tipo == "amazon" else "",
              "Fuera de tus filtros: te aviso porque es una preventa en español" if tipo == "preventa_es"
              else f"Filtro: {alerta.get('nombre', 'sin nombre')}"]
    mensaje = {
        "title": ("❤️ " if favorito else "") + titulo,
        "message": "\n".join(l for l in lineas if l),
        "click": item["url"],
        "priority": 5 if favorito else 4,  # 5 = urgente: ntfy la hace sonar con más insistencia
        "tags": [etiqueta],
        "actions": [{"action": "view", "label": {"preventa_es": "Reservar", "amazon": "Pedir invitación"}.get(tipo, "Comprar"), "url": item["url"]}],
    }
    if tipo == "amazon":
        mensaje["actions"].append({"action": "view", "label": "StockTCG", "url": f"{BASE}/amazon"})
    elif item.get("ppage"):
        mensaje["actions"].append({"action": "view", "label": "StockTCG", "url": BASE + item["ppage"]})
    mensaje["actions"].append({"action": "view", "label": "Ver en la app",
                               "url": f"{URL_LISTA}#lanzamientos" if tipo == "preventa_es" else f"{URL_LISTA}#amazon" if tipo == "amazon"
                               else url_coleccion(alerta.get("nombre", ""))})
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
    relampago = tiendas_relampago() if eventos else set()
    es_fav = lambda ev: grupo_de(ev[1], ev[2]) in favoritos
    eventos = sorted(eventos, key=lambda ev: not es_fav(ev))  # favoritos primero
    for ev in eventos[:MAX_AVISOS_SUELTOS]:
        tipo, item, alerta, precio_antes = ev
        print(f"  -> [{tipo}{' ❤' if es_fav(ev) else ''}] {item.get('setName')} · {item.get('fmtName')} · {formatear_precio(item)} · {item.get('store')}")
        enviar_ntfy(aviso_producto(tipo, item, alerta, precio_antes, es_fav(ev), item["store"] in relampago))

    resto = eventos[MAX_AVISOS_SUELTOS:]
    if resto:
        iconos = {"reposicion": "🟢", "bajada": "📉", "chollo": "🔥", "filtro": "🎯", "preventa_es": "🗓️", "amazon": "📨"}
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


def leer_amazon():
    """Campañas de Amazon por invitación abiertas (https://stocktcg.net/amazon). Cada una con la forma
    de un producto del catálogo para poder aplicarle los filtros. Los enlaces se dejan como vienen de
    StockTCG (con su código de afiliado: es como se financia el servicio)."""
    pagina = descargar_html(f"{BASE}/amazon")
    if "invitaci" not in pagina.lower():
        raise RuntimeError("la página de Amazon no tiene el formato esperado (¿ha cambiado la web?)")
    campanas = []
    for bloque in re.findall(r'<article class="ccard[^"]*">(.*?)</article>', pagina, re.S):
        if 'class="invite"' not in bloque:
            continue
        url = re.search(r'<a class="cbuy" href="([^"]+)"', bloque) or re.search(r'<a class="cimg" href="([^"]+)"', bloque)
        precio = re.search(r'<div class="cprice"><b>([\d.,]+)', bloque)
        if not (url and precio):
            continue
        slug = re.search(r'data-producto="([^"]*)"', bloque)
        slug = slug.group(1) if slug else ""
        coleccion = re.search(r'<a class="cset"[^>]*>(.*?)</a>', bloque, re.S)
        meta = re.search(r'<div class="cmeta"><span>(.*?)</span><span class="badge">(.*?)</span>', bloque, re.S)
        titulo = re.search(r'<div class="cmeta ctitle">(.*?)</div>', bloque, re.S)
        img = re.search(r'<img src="([^"]+)"', bloque)
        campanas.append({
            "store": "Amazon", "url": html_lib.unescape(url.group(1)), "available": True, "preorder": False,
            "title": texto_html(titulo.group(1)) if titulo else "", "setName": texto_html(coleccion.group(1)) if coleccion else "",
            "fmt": slug.split("--")[-1] if "--" in slug else "otros", "fmtName": texto_html(meta.group(1)) if meta else "",
            "langs": texto_html(meta.group(2)).lower() if meta else "es", "currency": "EUR",
            "price": float(precio.group(1).replace(".", "").replace(",", ".")),
            "img": html_lib.unescape(img.group(1)) if img else None, "ppage": f"/p/{slug}" if slug else "",
        })
    return campanas


def obtener_amazon(anterior):
    """Campañas de Amazon guardadas en el estado; se renuevan cada AMAZON_CADA."""
    guardado = (anterior or {}).get("amazon") or {}
    try:
        viejo = datetime.now(MADRID) - datetime.fromisoformat(guardado["ts"]) > AMAZON_CADA
    except (KeyError, ValueError, TypeError):
        viejo = True
    if not viejo:
        return guardado
    try:
        campanas = leer_amazon()
        print(f"Invitaciones de Amazon: {len(campanas)} campañas abiertas.")
        return {"ts": datetime.now(MADRID).isoformat(timespec="minutes"), "campanas": campanas}
    except Exception as e:  # sin Amazon el monitor sigue funcionando
        print(f"  (no se ha podido leer la página de Amazon: {e})")
        return guardado or {"ts": None, "campanas": []}


def escribir_amazon(campanas, filtro_de, fotos, ts):
    contenido = {"campanas": [{
        "titulo": c["title"], "coleccion": c["setName"], "tipo": c["fmt"], "tipoNombre": c["fmtName"],
        "precio": c["price"], "idioma": c["langs"], "url": c["url"], "img": c.get("img"),
        "foto": (fotos or {}).get(c.get("img")), "stocktcg": BASE + c["ppage"] if c.get("ppage") else None,
        "filtro": filtro_de.get(c["url"]),
    } for c in campanas]}
    if AMAZON.exists():
        try:
            viejo = json.loads(AMAZON.read_text(encoding="utf-8"))
            viejo.pop("actualizado", None)
            if viejo == contenido:
                return False
        except ValueError:
            pass
    AMAZON.parent.mkdir(exist_ok=True)
    AMAZON.write_text(json.dumps({"actualizado": ts, **contenido}, ensure_ascii=False, indent=1), encoding="utf-8")
    return True


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
            if nuevas >= MAX_FOTOS_NUEVAS:
                continue  # la web usará la foto original hasta la próxima comprobación
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

def escribir_datos_web(grupos, info, ts_catalogo, fotos=None, retraso=None):
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
    if retraso:
        contenido["retraso"] = retraso  # StockTCG atascado desde esta hora: stock leído en las tiendas

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

# ---------------------------------------------------------------- respaldo: leer las tiendas directamente

URL_PULSO = f"{BASE}/api/pulse.json"  # últimas reposiciones en directo (lo usa la portada de StockTCG)
NAVEGADOR = "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Mobile Safari/537.36"
LD_JSON = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)
META = re.compile(r"<meta\b[^>]*>", re.I)
HAY_STOCK = ("instock", "limitedavailability", "onlineonly", "instoreonly", "preorder", "presale")
SIN_STOCK = ("outofstock", "soldout", "discontinued", "backorder")
LECTURA_MAX_SEGUNDOS = 150      # tiempo máximo leyendo tiendas en una comprobación
LECTURA_MINIMA = 0.3            # si se lee menos de esta parte, el respaldo no es fiable


def leer_pagina(url):
    url = urllib.parse.quote(url, safe=":/?&=%#+,;@!$'()*~")
    req = urllib.request.Request(url, headers={"User-Agent": NAVEGADOR, "Accept-Language": "es-ES,es;q=0.9",
                                               "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        datos = resp.read(1_500_000)
        if resp.headers.get("Content-Encoding") == "gzip":
            datos = gzip.decompress(datos)
    return datos.decode("utf-8", "replace")


def disponibilidad(texto):
    t = normalizar(str(texto or "")).replace(" ", "").replace("_", "")
    if any(x in t for x in SIN_STOCK):
        return False
    if any(x in t for x in HAY_STOCK):
        return True
    return None


def productos_ld(nodo):
    if isinstance(nodo, list):
        for n in nodo:
            yield from productos_ld(n)
    elif isinstance(nodo, dict):
        tipo = nodo.get("@type")
        if tipo in ("Product", "ProductGroup") or (isinstance(tipo, list) and "Product" in tipo):
            yield nodo
        for k in ("@graph", "mainEntity"):
            if k in nodo:
                yield from productos_ld(nodo[k])


def leer_producto(item):
    """Mira la página del producto en la tienda. Devuelve (disponible, precio) o (None, None) si no se sabe.
    Solo se fía de datos estructurados (Shopify o schema.org), nunca de textos sueltos de la página."""
    url = item["url"]
    if "/products/" in url:  # Shopify
        try:
            d = json.loads(leer_pagina(url.split("?")[0].split("#")[0].rstrip("/") + ".js"))
            precios = {v.get("price") for v in d.get("variants") or []}
            precio = d["price"] / 100 if len(precios) <= 1 and isinstance(d.get("price"), int) else None
            return bool(d.get("available")), precio
        except Exception:
            pass
    html = leer_pagina(url)
    palabras = set(normalizar(item.get("title")).split())
    candidatos = []
    for bloque in LD_JSON.findall(html):
        try:
            datos = json.loads(bloque.strip())
        except ValueError:
            continue
        for prod in productos_ld(datos):
            ofertas = prod.get("offers") or []
            ofertas = ofertas if isinstance(ofertas, list) else [ofertas]
            ofertas = [o for o in ofertas if isinstance(o, dict)]
            estados = [e for o in ofertas if (e := disponibilidad(o.get("availability"))) is not None]
            if not estados:
                continue
            precio = next((o.get("price") or o.get("lowPrice") for o in ofertas if o.get("price") or o.get("lowPrice")), None)
            parecido = len(palabras & set(normalizar(prod.get("name")).split()))
            candidatos.append((parecido, any(estados), precio))
    if candidatos:
        _, disp, precio = max(candidatos, key=lambda c: c[0])  # el producto de la página, no los relacionados
        try:
            precio = float(str(precio).replace(",", ".")) if precio is not None else None
        except ValueError:
            precio = None
        return disp, precio
    for meta in META.findall(html):
        if re.search(r"(product|og):availability", meta, re.I):
            m = re.search(r'content="([^"]+)"', meta, re.I)
            if m and (disp := disponibilidad(m.group(1))) is not None:
                return disp, None
    return None, None


def leer_tiendas(items):
    """Lee la disponibilidad de cada producto en su tienda. Cada tienda se lee de una en una (sin
    peticiones en paralelo a la misma tienda), y varias tiendas a la vez. Devuelve {clave: (disp, precio)}."""
    from concurrent.futures import ThreadPoolExecutor
    import time
    limite = time.monotonic() + LECTURA_MAX_SEGUNDOS
    por_tienda = {}
    for i in items:
        por_tienda.setdefault(i["store"], []).append(i)

    def una_tienda(lista):
        res, fallos = {}, 0
        for i in lista:
            if time.monotonic() > limite or fallos >= 3:
                break  # tienda que no responde o bloquea: se deja como estaba
            try:
                disp, precio = leer_producto(i)
            except Exception:
                fallos += 1
                continue
            if disp is not None:
                res[clave(i)] = (disp, precio)
        return res

    lecturas = {}
    with ThreadPoolExecutor(16) as ex:
        for res in ex.map(una_tienda, por_tienda.values()):
            lecturas.update(res)
    return lecturas


# ---------------------------------------------------------------- segunda fuente: stocktcg.es

ES_BASE = "https://stocktcg.es"
# categoría de stocktcg.es -> (fmt, fmtName) como los usa stocktcg.net
ES_FORMATOS = {"booster box": ("booster-box", "Booster Box / Display"), "etb": ("etb", "ETB"),
               "bundle": ("booster-bundle", "Booster Bundle"), "sobre": ("booster-pack", "Sobre / Pack"),
               "blister": ("blister", "Blister"), "lata": ("tin", "Tin"), "coleccion": ("collection-box", "Collection Box"),
               "mazo": ("deck", "Deck"), "otro": ("otros", "Otros")}


def url_comparable(url):
    p = urllib.parse.urlsplit((url or "").strip())
    return p.netloc.lower().removeprefix("www.") + urllib.parse.unquote(p.path).rstrip("/").lower()


def leer_stocktcg_es():
    """Lee las páginas de comparador de Pokémon en español de stocktcg.es (otra web, independiente de
    stocktcg.net). Cada una trae las tiendas con stock (precio, enlace) en datos schema.org.
    Devuelve la lista de ofertas o None si no se ha podido leer entera (así no hay falsos avisos)."""
    from concurrent.futures import ThreadPoolExecutor
    try:
        mapa = leer_pagina(ES_BASE + "/sitemap.xml")
    except Exception as e:
        print("  stocktcg.es no responde:", e)
        return None
    paginas = [u for u in re.findall(r"<loc>([^<]*)</loc>", mapa) if "/comparador/pokemon-" in u and u.endswith("-es")]
    if not paginas:
        return None

    def una(url):
        try:
            html = leer_pagina(url)
        except Exception:
            return None
        for bloque in LD_JSON.findall(html):
            try:
                datos = json.loads(bloque)
            except ValueError:
                continue
            for prod in productos_ld(datos):
                ofertas = (prod.get("offers") or {}).get("offers") or []
                fmt, fmt_nombre = ES_FORMATOS.get(normalizar(prod.get("category")), ("otros", "Otros"))
                lista = []
                for o in ofertas:
                    disp = normalizar(str(o.get("availability"))).replace(" ", "")
                    if not o.get("url") or not ("instock" in disp or "preorder" in disp):
                        continue
                    try:
                        precio = round(float(o.get("price")), 2)
                    except (TypeError, ValueError):
                        continue
                    lista.append({"title": prod.get("name"), "setName": "", "fmt": fmt, "fmtName": fmt_nombre,
                                  "langs": "es", "store": (o.get("seller") or {}).get("name") or "?", "url": o["url"],
                                  "price": precio, "currency": o.get("priceCurrency") or "EUR", "available": True,
                                  "preorder": "preorder" in disp, "img": prod.get("image"), "fuente": url})
                return lista
        return None

    with ThreadPoolExecutor(8) as ex:
        resultados = list(ex.map(una, paginas))
    fallidas = sum(r is None for r in resultados)
    if fallidas:
        print(f"  stocktcg.es: {fallidas} de {len(paginas)} páginas sin leer; no se usa en esta comprobación.")
        return None
    ofertas = [o for r in resultados for o in r]
    print(f"stocktcg.es: {len(ofertas)} ofertas en stock en {len(paginas)} productos de Pokémon en español.")
    return ofertas


def mezclar_stocktcg_es(items, ofertas, alertas, general, respaldo, anterior):
    """Añade al catálogo lo que aporta stocktcg.es:
    - ofertas de tiendas (dominios) que stocktcg.net no vigila: se añaden como productos (en la misma ficha de la app
      que su producto de stocktcg.net, si existe);
    - con StockTCG atascado, las ofertas de tiendas que no se han podido leer marcan el producto como disponible.
    Devuelve (items, extras, tocados)."""
    def dominio(url):
        return urllib.parse.urlsplit(url or "").netloc.lower().removeprefix("www.")

    dominios_net = {dominio(i.get("url")) for i in items if i.get("url")}  # tiendas que vigila stocktcg.net
    tocados, extras = set(), []
    if ofertas is not None:
        indice = {url_comparable(i["url"]): n for n, i in enumerate(items) if i.get("url")}
        hermanos = {}  # (filtro, fmt) -> productos de stocktcg.net para copiar colección, ficha y Cardmarket
        for i in items:
            if i.get("url") and (a := primer_filtro({**i, "available": True}, alertas, general)):
                hermanos.setdefault((a.get("nombre"), i.get("fmt")), []).append(i)
        vistos = set()
        for o in ofertas:
            alerta = primer_filtro(o, alertas, general)
            if not alerta:
                continue
            if dominio(o["url"]) in dominios_net:
                n = indice.get(url_comparable(o["url"]))
                if respaldo and n is not None and clave(items[n]) not in respaldo["leidos"] and not items[n].get("available"):
                    items[n] = {**items[n], "available": True}
                    tocados.add(clave(items[n]))
                continue
            if clave(o) in vistos:
                continue
            vistos.add(clave(o))
            familia = hermanos.get((alerta.get("nombre"), o["fmt"])) or []
            if familia:
                paginas = Counter(h.get("ppage") for h in familia if h.get("ppage"))
                base = next((h for h in familia if h.get("ppage") == paginas.most_common(1)[0][0]), familia[0]) if paginas else familia[0]
                o = {**o, "setName": base.get("setName"), "fmtName": base.get("fmtName"), "ppage": base.get("ppage"),
                     "cmTrend": base.get("cmTrend"), "cmPctTrend": base.get("cmPctTrend")}
            extras.append(o)
    else:
        extras = []

    # Si stocktcg.es falla, o una oferta desaparece una sola vez, se mantiene lo anterior (evita avisos repetidos)
    actuales = {clave(o) for o in extras}
    for o in (anterior or {}).get("es_items") or []:
        if clave(o) not in actuales and (ofertas is None or o.get("faltas", 0) < 1):
            extras.append({**o, "faltas": 0 if ofertas is None else o.get("faltas", 0) + 1})
    extras = [{k: v for k, v in o.items() if k != "faltas"} | ({"faltas": o["faltas"]} if o.get("faltas") else {}) for o in extras]
    return items + extras, extras, tocados


def catalogo_de_respaldo(datos, general, alertas, motivo):
    """El catálogo de StockTCG está atascado: se pone al día leyendo directamente en cada tienda los
    productos de tus filtros. Lo que no se puede leer se completa con el feed de reposiciones de StockTCG
    (si avanza) o se queda como estaba. Devuelve el catálogo corregido y qué se ha tocado."""
    candidatos = [i for i in datos["items"] if i.get("url") and primer_filtro({**i, "available": True}, alertas, general)]
    lecturas = leer_tiendas(candidatos)
    print(f"Respaldo: leídos en la tienda {len(lecturas)} de {len(candidatos)} productos de tus filtros.")

    cambios = {}
    for i in candidatos:
        k = clave(i)
        if k in lecturas:
            disp, precio = lecturas[k]
            nuevo = {**i, "available": disp}
            if precio and i.get("price") and 0.5 <= precio / i["price"] <= 1.5:
                nuevo["price"] = round(precio, 2)
            cambios[k] = nuevo

    # Feed de reposiciones de StockTCG para los productos que no se pueden leer (p. ej. grandes superficies)
    ts_catalogo = datos.get("ts") or ""
    try:
        reposiciones = descargar_json(URL_PULSO).get("restocks") or []
    except RuntimeError:
        reposiciones = []
    indice = {clave(i): i for i in candidatos}
    del_pulso = set()
    for r in reposiciones:
        k = clave(r) if r.get("url") else None
        if k in indice and k not in lecturas and (r.get("ts") or "") > ts_catalogo:
            cambios[k] = {**indice[k], "available": True, "price": r.get("price") or indice[k].get("price")}
            del_pulso.add(k)

    if candidatos and len(lecturas) < LECTURA_MINIMA * len(candidatos) and not del_pulso:
        raise motivo  # ni las tiendas ni el feed: no hay forma fiable de seguir

    items = [cambios.get(clave(i), i) if i.get("url") else i for i in datos["items"]]
    leidas = {i["store"] for i in candidatos if clave(i) in lecturas}
    sin_leer = sorted({i["store"] for i in candidatos} - leidas)
    return {"motivo": str(motivo), "items": items, "tocados": set(cambios), "leidos": set(lecturas), "candidatos": len(candidatos),
            "tiendas_leidas": leidas, "sin_leer": sin_leer, "del_pulso": len(del_pulso)}


def comprobar(solo_resumen, anterior):
    """Hace una comprobación completa. Devuelve el nuevo estado a guardar (o None si no hay que guardarlo)."""
    general, alertas, todas_alertas, alertas_app = cargar_filtros(anterior)
    print(f"Filtros activos: {len(alertas)}")

    datos = descargar_json(STOCK_URL)
    respaldo = None
    try:
        validar_catalogo(datos)
    except CatalogoAtascado as e:
        if not anterior:
            raise
        respaldo = catalogo_de_respaldo(datos, general, alertas, e)
        datos = {**datos, "items": respaldo["items"],
                 "stale_stores": [t for t in datos.get("stale_stores", []) if t not in respaldo["tiendas_leidas"]]}

    en_respaldo_antes = bool(anterior and (anterior.get("modo_respaldo") or anterior.get("modo_pulso")))
    if respaldo and not en_respaldo_antes and not solo_resumen:
        sin_leer = respaldo["sin_leer"]
        enviar_ntfy({"title": "🟡 StockTCG va con retraso: sigo vigilando yo las tiendas",
                     "message": f"{respaldo['motivo']}\nMientras tanto leo directamente en cada tienda {len(respaldo['leidos'])} de "
                                f"{respaldo['candidatos']} productos de tus colecciones y te aviso igual de las reposiciones."
                                + (f" No dejan leerse: {', '.join(sin_leer[:6])}{'…' if len(sin_leer) > 6 else ''}." if sin_leer else "")
                                + " En este modo no aparecen productos nuevos. Te avisaré cuando StockTCG vuelva.",
                     "click": URL_LISTA, "tags": ["hourglass"]})
    if not respaldo and en_respaldo_antes and not solo_resumen:
        enviar_ntfy({"title": "✅ StockTCG vuelve a actualizar su catálogo",
                     "message": "Vuelvo al modo normal: todas las tiendas, productos nuevos y bajadas de precio.",
                     "click": URL_LISTA, "tags": ["white_check_mark"]})
    # Al entrar en el respaldo se avisa de lo que se ha repuesto desde que StockTCG se paró, pero no de
    # bajadas (el precio de la tienda y el de StockTCG pueden diferir sin que haya cambiado). Al salir,
    # lo leído en las tiendas no se vuelve a avisar: solo cambia la fuente.
    sin_bajadas = respaldo["tocados"] if respaldo and not en_respaldo_antes else set()
    silenciar = set((anterior or {}).get("respaldo_leidos") or []) if not respaldo and en_respaldo_antes else set()

    ofertas_es = leer_stocktcg_es()
    items, es_items, tocados_es = mezclar_stocktcg_es(list(datos["items"]), ofertas_es, alertas, general, respaldo, anterior)
    print(f"stocktcg.es aporta {len(es_items)} ofertas de tiendas que stocktcg.net no vigila"
          + (f" y {len(tocados_es)} reposiciones de tiendas que no se dejan leer" if tocados_es else "") + ".")
    if respaldo:
        respaldo["tocados"] |= tocados_es
        if not en_respaldo_antes:
            sin_bajadas = respaldo["tocados"]
    if anterior is not None and "es_items" not in anterior:
        silenciar = silenciar | {clave(o) for o in es_items}  # primera vez con stocktcg.es: no es reposición
    obsoletas = set(datos.get("stale_stores", []))
    print(f"Catálogo de {datos.get('ts')}: {len(items)} productos ({len(obsoletas)} tiendas sin actualizar)")

    disponibles = [i for i in items if i.get("available") and i.get("url")]
    coinciden = unificar_fichas([(i, a) for i in disponibles if (a := primer_filtro(i, alertas, general))])
    grupos = agrupar(coinciden, alertas)
    for nombre, lista in grupos.items():
        print(f"  {nombre}: {len(lista)} en stock")

    huella = huella_filtros(general, todas_alertas)
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

        if misma_config and item["store"] not in obsoletas and k not in silenciar:
            if k not in antes:
                eventos.append(("reposicion", item, alerta, None))
                desde, precio_antes, ts_bajada = ahora, None, None
            elif seguidos_antes is not None and previo[0] is None:
                eventos.append(("filtro", item, alerta, None))
                desde = ahora
            elif seguidos_antes is not None:
                if k in sin_bajadas:
                    pass
                elif avisar_bajadas and previo[0] and item["price"] < previo[0] * (1 - bajada_minima / 100):
                    eventos.append(("bajada", item, alerta, previo[0]))
                    precio_antes, ts_bajada = previo[0], ahora
                elif avisar_chollos and es_chollo(item) and previo[1] not in NIVELES_CHOLLO:
                    eventos.append(("chollo", item, alerta, None))

        if precio_antes is not None and item["price"] >= precio_antes:
            precio_antes, ts_bajada = None, None  # ha vuelto a subir: ya no es una bajada
        seguidos[k] = [item["price"], item.get("cmTier"), item["store"], desde, precio_antes, ts_bajada]
        info[k] = (desde, precio_antes, ts_bajada)

    calendario = obtener_calendario(anterior)
    amazon = obtener_amazon(anterior)
    campanas = amazon.get("campanas", [])
    filtro_amazon = {c["url"]: a for c in campanas if (a := primer_filtro(c, alertas, general))}
    lanzamientos, preventas_es = cruzar_lanzamientos(calendario, disponibles)

    imagenes = {i["img"] for i, _ in coinciden if i.get("img")}
    imagenes |= {i["img"] for i in preventas_es if i.get("img")}
    imagenes |= {i["img"] for l in lanzamientos for i in l["items"] if i.get("img")}
    imagenes |= {l["img"] for l in lanzamientos if l.get("img")}
    imagenes |= {c["img"] for c in campanas if c.get("img")}
    fotos = preparar_fotos(imagenes)
    if escribir_datos_web(grupos, info, ahora if respaldo else datos.get("ts"), fotos,
                          retraso=datos.get("ts") if respaldo else None):
        print("datos/stock.json actualizado.")
    if escribir_amazon(campanas, {u: a.get("nombre") for u, a in filtro_amazon.items()}, fotos, amazon.get("ts")):
        print("datos/amazon.json actualizado.")
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

    # Campañas de Amazon por invitación nuevas que cumplen tus filtros
    if anterior and anterior.get("amazon_vistas") is not None and general.get("avisar_amazon", True):
        vistas_amazon = set(anterior["amazon_vistas"])
        for c in campanas:
            if c["url"] in filtro_amazon and c["url"] not in vistas_amazon:
                eventos.append(("amazon", c, filtro_amazon[c["url"]], None))

    nuevo_estado = {"ts": datos.get("ts"), "filtros": huella, "tiendas": tiendas, "seguidos": seguidos, "errores": 0,
                    "preventas_es": sorted({clave(i) for i in preventas_es}), "calendario": calendario,
                    "alertas_app": alertas_app, "amazon": amazon, "es_items": es_items,
                    # si la página nunca se ha podido leer, no se toca la lista (así no llegan avisos de todas de golpe)
                    "amazon_vistas": sorted(c["url"] for c in campanas) if amazon.get("ts") else (anterior or {}).get("amazon_vistas")}
    if respaldo:
        nuevo_estado["modo_respaldo"] = (anterior or {}).get("modo_respaldo") or ahora
        nuevo_estado["respaldo_leidos"] = sorted(respaldo["leidos"] | respaldo["tocados"])

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


# ---------------------------------------------------------------- vigilante del propio sistema

CRONJOB_PARADO = timedelta(minutes=20)    # sin lanzamientos de cron-job.org durante esto = parado
REPETIR_PARADO = timedelta(hours=6)       # recordatorio mientras siga parado
AVISOS_CADUCIDAD = (7, 3, 1, 0)           # días antes de que caduque la clave de cron-job.org


def caducidad_clave():
    try:
        with open(FILTROS, "rb") as f:
            valor = tomllib.load(f).get("general", {}).get("caducidad_clave_cronjob")
        return date.fromisoformat(str(valor)) if valor else None
    except (ValueError, OSError, tomllib.TOMLDecodeError):
        return None


def vigilar_sistema(anterior, nuevo):
    """Avisa si cron-job.org deja de lanzar las comprobaciones (solo queda el respaldo de GitHub, cada 30 min)
    y recuerda renovar la clave de GitHub que usa cron-job.org antes de que caduque."""
    ahora = datetime.now(MADRID)
    evento = os.environ.get("GITHUB_EVENT_NAME", "")  # workflow_dispatch = cron-job.org (o a mano); schedule = respaldo
    previo = anterior or {}
    ultima = previo.get("ultima_cronjob")
    nuevo["ultima_cronjob"] = ahora.isoformat(timespec="minutes") if evento == "workflow_dispatch" else ultima
    nuevo["cronjob_caido"] = previo.get("cronjob_caido")
    nuevo["aviso_caducidad"] = previo.get("aviso_caducidad")
    caduca = caducidad_clave()
    dias = (caduca - ahora.date()).days if caduca else None
    fecha_txt = f"{caduca.day}/{caduca.month}/{caduca.year}" if caduca else ""
    renovar = ("1) En github.com/settings/personal-access-tokens abre «cron-job stocktcg» y pulsa Regenerate token. "
               "2) En cron-job.org, en tu tarea, cambia el encabezado Authorization por «Bearer» + la clave nueva. "
               "3) Pon la nueva fecha de caducidad en filtros.toml (caducidad_clave_cronjob) o pídemelo.")

    if evento == "workflow_dispatch" and previo.get("cronjob_caido"):
        enviar_ntfy({"title": "✅ cron-job.org vuelve a lanzar las comprobaciones",
                     "message": "Vuelves a tener comprobaciones cada 5 minutos.", "click": URL_LISTA, "tags": ["white_check_mark"]})
        nuevo["cronjob_caido"] = None
    elif evento == "schedule" and ultima:
        parado = ahora - datetime.fromisoformat(ultima)
        ultimo_aviso = previo.get("cronjob_caido")
        if parado > CRONJOB_PARADO and (not ultimo_aviso or ahora - datetime.fromisoformat(ultimo_aviso) > REPETIR_PARADO):
            causa = (f"La clave de GitHub que usa caducó el {fecha_txt}: seguramente es eso. {renovar}" if dias is not None and dias < 0
                     else "Abre la tarea en cron-job.org y mira su historial: si responde 401, la clave de GitHub ha caducado o se ha borrado; "
                          "si la tarea está desactivada, actívala.")
            enviar_ntfy({"title": "⚠️ cron-job.org ha dejado de lanzar las comprobaciones",
                         "message": f"Lleva {round(parado.total_seconds() / 3600, 1)} h sin lanzarlas. Solo funciona el respaldo de GitHub "
                                    f"(cada 30 min), así que los avisos pueden llegar con retraso.\n{causa}",
                         "click": "https://console.cron-job.org/jobs", "priority": 4, "tags": ["warning"]})
            nuevo["cronjob_caido"] = ahora.isoformat(timespec="minutes")
            print("Aviso: cron-job.org parece parado.")

    if dias is not None and dias in AVISOS_CADUCIDAD and nuevo.get("aviso_caducidad") != ahora.date().isoformat():
        cuando = "hoy" if dias == 0 else ("mañana" if dias == 1 else f"en {dias} días")
        enviar_ntfy({"title": f"🔑 La clave de cron-job.org caduca {cuando} ({fecha_txt})",
                     "message": f"Si caduca, las comprobaciones pasarán de cada 5 a cada 30 minutos. Para renovarla:\n{renovar}",
                     "click": "https://github.com/settings/personal-access-tokens", "priority": 4, "tags": ["key"]})
        nuevo["aviso_caducidad"] = ahora.date().isoformat()
        print(f"Aviso: la clave de cron-job.org caduca {cuando}.")


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
        if errores_antes >= AVISAR_FALLO_TRAS and not nuevo.get("modo_respaldo"):
            enviar_ntfy({"title": "✅ El monitor de stock vuelve a funcionar",
                         "message": f"Ha estado fallando unos {errores_antes * 5} minutos. Ya vuelves a recibir avisos.",
                         "click": URL_LISTA, "tags": ["white_check_mark"]})
        try:
            vigilar_sistema(anterior, nuevo)
        except Exception as e:  # el vigilante nunca debe romper la comprobación
            print("  (vigilante del sistema:", e, ")")
        ESTADO.parent.mkdir(exist_ok=True)
        ESTADO.write_text(json.dumps(nuevo))


if __name__ == "__main__":
    main()
