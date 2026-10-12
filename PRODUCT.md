# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Un único usuario: coleccionista de Pokémon TCG en España que compra producto sellado en español. Usa la app en el móvil (Samsung S23 FE, Chrome, instalada como PWA). Su trabajo principal al abrirla es **ver qué ha entrado nuevo** y comprar rápido antes de que se agote; muchas veces llega desde una notificación de ntfy.

## Product Purpose

Mostrar, con los datos de StockTCG filtrados por sus colecciones (Fuegos Fantasmales, Evoluciones Prismáticas, Héroes Ascendentes, 30 Aniversario solo ETB y booster bundle), todo el stock en español disponible ahora, destacando lo recién repuesto, las bajadas de precio y los precios por debajo de Cardmarket, y llevar a la tienda en un toque. Éxito: de la notificación o del icono a la página de compra de la tienda en dos toques.

## Positioning

No es una tienda ni otro agregador general: es su propio vigilante, limitado a lo que él colecciona, con la memoria de cuándo entró cada oferta y cuánto bajó.

## Operating Context

- Los datos los genera `monitor.py` cada 5 minutos (cron-job.org → GitHub Actions) en `datos/stock.json`; la web (`docs/`, GitHub Pages) los lee de raw.githubusercontent.com.
- Las notificaciones (ntfy) abren la web en la colección del aviso (`#Nombre`).
- Uso de un vistazo, a menudo con prisa; el usuario compara entre tiendas antes de comprar.

## Capabilities and Constraints

- Sitio estático, sin framework ni build: HTML/CSS/JS en un solo `docs/index.html`, más manifest y service worker (instalable).
- Datos por oferta: título de la tienda, colección, tipo, precio y moneda, tienda, idioma, url, imagen (servida por la tienda o tcgplayer), preventa, precio de tendencia de Cardmarket y % frente a él, nivel chollo, enlace a StockTCG, desde cuándo está en stock, precio anterior y fecha de bajada.
- El mismo producto aparece en varias tiendas: se agrupa en una ficha por producto (decidido por el usuario). StockTCG agrupa por colección+tipo (`/p/...`), lo que es un producto único para ETB, booster box, booster bundle, displays y UPC, pero no para collection box, tins, blisters o sobres (varios productos distintos comparten página).
- "Desde" y bajadas solo existen desde que el monitor empezó a registrarlos (11/10/2026).
- No hay datos de unidades disponibles ni de gastos de envío.

## Brand Commitments

- Nombre: "Stock TCG". Logo elegido por el usuario: el logo del 30 aniversario de Pokémon (Pikachu con el "30"). `docs/logo-30.png` (fondo transparente, cabecera) y `docs/icono-30-*.png` (iconos de la app sobre blanco). Es un logo oficial de Pokémon, de uso personal.
- Referencias elegidas por el usuario para la calidad y la forma de uso: **Wallapop / Vinted** (cuadrícula de fotos, precio destacado, filtros en chips) e **Idealo / PriceRunner** (ficha por producto, "desde X €", tiendas ordenadas por precio). Camino convencional del sector, ejecutado con su nivel de acabado.
- Idioma: español de España, tono directo.

## Evidence on Hand

Datos reales en `datos/stock.json` (≈80 ofertas). No hay valoraciones de tiendas, gastos de envío ni stock por unidades: no inventarlos.

## Product Principles

1. Lo nuevo primero: lo que acaba de entrar se ve antes que nada.
2. Dos toques hasta la tienda.
3. Comparar sin pensar: el precio más bajo siempre visible, el resto ordenado.
4. Datos honestos: cuándo se actualizó, de dónde viene el precio de referencia, nada inventado.
