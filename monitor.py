"""Vigila el stock de stocktcg.net y manda notificaciones al móvil (ntfy) cuando
un producto que cumple tus filtros (filtros.toml) vuelve a estar disponible.
Además mantiene STOCK.md con todo lo que hay en stock ahora mismo según tus filtros.

Uso:
    python monitor.py            # comprobación normal (la que hace GitHub cada pocos minutos)
    python monitor.py --resumen  # manda un resumen de todo lo que hay en stock ahora mismo
                                 # (no toca el estado de reposiciones)

Variables de entorno:
    NTFY_TOPIC   tema de ntfy al que estás suscrito en el móvil (obligatoria para avisar)
    NTFY_SERVER  servidor ntfy (por defecto https://ntfy.sh)
"""

import gzip
import hashlib
import json
import os
import re
import sys
import tomllib
import unicodedata
import urllib.request
from datetime import datetime
from pathlib import Path

BASE = "https://stocktcg.net"
STOCK_URL = f"{BASE}/stock.json"
USER_AGENT = "stocktcg-alertas/1.0 (monitor personal; github.com/YbaNiN)"

DIR = Path(__file__).parent
FILTROS = DIR / "filtros.toml"
ESTADO = DIR / "estado" / "estado.json"
LISTA = DIR / "STOCK.md"

# Enlace a STOCK.md en GitHub (GitHub Actions define estas variables)
REPO = os.environ.get("GITHUB_REPOSITORY")
URL_LISTA = f"https://github.com/{REPO}/blob/main/STOCK.md" if REPO else f"{BASE}/stock"

MAX_AVISOS_SUELTOS = 8   # si hay más reposiciones a la vez, el resto va en un resumen
MAX_LINEAS_RESUMEN = 30  # líneas por notificación de resumen

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


def formatear_precio(item):
    precio = f"{item['price']:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    moneda = item.get("currency") or "EUR"
    return f"{precio} €" if moneda == "EUR" else f"{precio} {moneda}"


def orden_item(item):
    fmt = item.get("fmt")
    pos = ORDEN_FORMATOS.index(fmt) if fmt in ORDEN_FORMATOS else len(ORDEN_FORMATOS)
    return pos, precio_euros(item) or 0


def descargar_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        datos = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            datos = gzip.decompress(datos)
    return json.loads(datos)


# ---------------------------------------------------------------- filtros

def cargar_filtros():
    with open(FILTROS, "rb") as f:
        cfg = tomllib.load(f)
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


def aviso_producto(item, alerta):
    preventa = item.get("preorder")
    titulo = f"{'🕒 Preventa' if preventa else '🟢 En stock'}: {item.get('setName')} · {item.get('fmtName')}"
    lineas = [
        f"{formatear_precio(item)} en {item.get('store')} · {(item.get('langs') or '?').upper()}",
        item.get("title") or "",
    ]
    if item.get("cmTrend"):
        pct = item.get("cmPctTrend")
        lineas.append(f"Cardmarket: {item['cmTrend']:.2f} €" + (f" ({pct:+.0f}%)" if pct is not None else ""))
    lineas.append(f"Filtro: {alerta.get('nombre', 'sin nombre')}")

    mensaje = {
        "title": titulo,
        "message": "\n".join(lineas),
        "click": item["url"],
        "priority": 4,
        "tags": ["hourglass" if preventa else "rotating_light"],
        "actions": [{"action": "view", "label": "Comprar", "url": item["url"]}],
    }
    if item.get("ppage"):
        mensaje["actions"].append({"action": "view", "label": "StockTCG", "url": BASE + item["ppage"]})
    mensaje["actions"].append({"action": "view", "label": "Todo el stock", "url": URL_LISTA})
    if item.get("img"):
        mensaje["attach"] = item["img"]
    return mensaje


def linea_corta(item):
    preventa = " (preventa)" if item.get("preorder") else ""
    return f"• {item.get('fmtName')}{preventa} · {formatear_precio(item)} · {item.get('store')}"


def enviar_resumen(grupos, motivo):
    for nombre, items in grupos.items():
        lineas = [linea_corta(i) for i in items[:MAX_LINEAS_RESUMEN]]
        if len(items) > MAX_LINEAS_RESUMEN:
            lineas.append(f"… y {len(items) - MAX_LINEAS_RESUMEN} más (toca para ver la lista completa)")
        enviar_ntfy({
            "title": f"📦 {nombre}: {len(items)} en stock",
            "message": "\n".join(lineas) if lineas else "Ahora mismo no hay nada en stock con este filtro.",
            "click": URL_LISTA,
            "tags": ["package"],
            "actions": [{"action": "view", "label": "Ver lista con enlaces", "url": URL_LISTA}],
        })
    print(f"Resumen enviado ({motivo}).")


# ---------------------------------------------------------------- lista STOCK.md

def escapar_md(texto):
    return re.sub(r"([\\|\[\]])", r"\\\1", texto or "")


def escribir_lista(grupos, ts_catalogo):
    marca = "<!-- contenido -->"
    partes = []
    for nombre, items in grupos.items():
        partes.append(f"## {nombre} ({len(items)})\n")
        if not items:
            partes.append("_Ahora mismo no hay nada en stock con este filtro._\n")
            continue
        partes.append("| Tipo | Precio | Tienda | Producto |\n|---|---|---|---|")
        for i in items:
            tipo = i.get("fmtName") + (" · preventa" if i.get("preorder") else "")
            partes.append(f"| {escapar_md(tipo)} | {formatear_precio(i)} | {escapar_md(i.get('store'))} "
                          f"| [{escapar_md(i.get('title'))}]({i['url']}) |")
        partes.append("")
    contenido = "\n".join(partes)

    if LISTA.exists() and LISTA.read_text(encoding="utf-8").split(marca)[-1].strip() == contenido.strip():
        return False  # sin cambios: no se reescribe (así no hay commits innecesarios)

    hora = datetime.fromisoformat(ts_catalogo).strftime("%d/%m/%Y %H:%M") if ts_catalogo else "?"
    total = sum(len(v) for v in grupos.values())
    cabecera = (f"# Stock actual\n\n{total} productos en stock que cumplen tus filtros "
                f"([filtros.toml](filtros.toml)). Datos de [StockTCG]({BASE}/stock) del {hora} "
                f"(hora de España). Toca el nombre del producto para ir a la tienda.\n")
    LISTA.write_text(f"{cabecera}\n{marca}\n\n{contenido}", encoding="utf-8")
    return True


# ---------------------------------------------------------------- principal

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    solo_resumen = "--resumen" in sys.argv
    general, alertas = cargar_filtros()
    print(f"Filtros activos: {len(alertas)}")

    datos = descargar_json(STOCK_URL)
    items = datos.get("items", [])
    obsoletas = set(datos.get("stale_stores", []))
    print(f"Catálogo de {datos.get('ts')}: {len(items)} productos ({len(obsoletas)} tiendas sin actualizar)")

    disponibles = [i for i in items if i.get("available") and i.get("url")]
    coinciden = [(i, a) for i in disponibles if (a := primer_filtro(i, alertas, general))]
    grupos = agrupar(coinciden, alertas)
    for nombre, lista in grupos.items():
        print(f"  {nombre}: {len(lista)} en stock")

    if escribir_lista(grupos, datos.get("ts")):
        print("STOCK.md actualizado.")

    if solo_resumen:
        enviar_resumen(grupos, "pedido a mano")
        return

    # Estado: claves de productos disponibles por tienda en la comprobación anterior
    anterior = json.loads(ESTADO.read_text()) if ESTADO.exists() else None
    huella = huella_filtros()

    actual = {}
    for item in disponibles:
        actual.setdefault(item["store"], []).append(clave(item))
    if anterior:
        # Las tiendas sin datos recientes conservan su estado anterior para no dar falsos avisos
        for tienda in obsoletas:
            if tienda in anterior["tiendas"]:
                actual[tienda] = anterior["tiendas"][tienda]

    ESTADO.parent.mkdir(exist_ok=True)
    ESTADO.write_text(json.dumps({"ts": datos.get("ts"), "filtros": huella, "tiendas": actual}))

    if anterior is None or anterior.get("filtros") != huella:
        # Primera vez o filtros cambiados: resumen de lo que ya hay (y no se avisa como reposición)
        enviar_resumen(grupos, "primera ejecución" if anterior is None else "filtros cambiados")
        return

    antes = {k for claves in anterior["tiendas"].values() for k in claves}
    nuevos = [(i, a) for i, a in coinciden if i["store"] not in obsoletas and clave(i) not in antes]
    print(f"Reposiciones que cumplen tus filtros: {len(nuevos)}")

    for item, alerta in nuevos[:MAX_AVISOS_SUELTOS]:
        print(f"  -> {item.get('setName')} · {item.get('fmtName')} · {formatear_precio(item)} · {item.get('store')}")
        enviar_ntfy(aviso_producto(item, alerta))

    resto = nuevos[MAX_AVISOS_SUELTOS:]
    if resto:
        lineas = [f"• {i.get('setName')} · {i.get('fmtName')} · {formatear_precio(i)} · {i.get('store')}" for i, _ in resto[:15]]
        if len(resto) > 15:
            lineas.append(f"… y {len(resto) - 15} más")
        enviar_ntfy({"title": f"🟢 {len(resto)} reposiciones más", "message": "\n".join(lineas),
                     "click": URL_LISTA, "priority": 4, "tags": ["rotating_light"]})


if __name__ == "__main__":
    main()
