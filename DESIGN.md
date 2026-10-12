# Design

Sistema visual de la web-app (`docs/index.html`). Describe lo construido; si cambias el diseño, actualiza este archivo.

## Mundo

Catálogo tipo Vinted/Wallapop (fondo blanco, fotos de producto en pozos grises, precio en negrita, filtros en chips, pestañas subrayadas) con la ficha de un comparador tipo Idealo (una ficha por producto, "desde X €", tiendas ordenadas por precio con "Ir a la tienda"). Modo Operate: lo familiar es una virtud; la marca vive en los detalles.

## Color (Restrained)

Neutros más un único acento verde, que se usa solo para acciones (Comprar, Ir a la tienda, enlaces) y para el estado "nuevo". Los estados usan parejas fondo tintado + texto:

| Rol | Claro | Oscuro |
|---|---|---|
| Fondo `--bg` | #ffffff | #101215 |
| Pozo de foto / campo `--well` / `--chip` | #f2f3f5 | #1b1e23 / #1f2328 (las fotos usan #eceef1 en oscuro) |
| Texto / secundario / tenue | #16181d / #5d636d / #8a9099 | #eceef1 / #a1a8b2 / #7d848e |
| Líneas `--line` | #e6e8eb | #282c32 |
| Acento `--accent` | #0b7a5a (texto blanco) | #2fb487 (texto #06281c) |
| Nuevo | #e2f3ec / #0a6649 | #123a2c / #6fdcb2 |
| Bajada de precio | #fdeceb / #b42318 | #42191a / #ff9a92 |
| Bajo Cardmarket | #fff3d6 / #855000 | #3d2c0a / #ffc861 |
| Preventa | #eceef2 / #3e4550 | #262a30 / #c4cad3 |

Claro u oscuro según el ajuste del móvil (`prefers-color-scheme`).

## Tipografía

Fuente del sistema (system-ui), una sola familia. Escala fija: 12 / 13 / 14 / 15 / 17 / 19 px. Precios en 750 con `tabular-nums`. Títulos de sección 19 px, -0.015em.

## Componentes

- **Barra superior fija:** logo del 30 aniversario (`logo-30.png`, 32 px de alto, fondo transparente) + "Stock TCG", estado "Comprobado hace X min" (punto verde; ámbar si pasan más de 30 min o no hay conexión), actualizar; buscador en campo gris de 42 px; pestañas de colección subrayadas con contador.
- **Novedades:** filas (miniatura 64 px, evento con icono y hora, producto, colección · tienda, precio, botón Comprar). 4 visibles y "Ver todas". En escritorio, 2 columnas.
- **Herramientas:** select de orden con forma de chip, separador y chips de tipo (activo = relleno de color texto). Scroll horizontal en móvil; en ≥900 px pasan a varias líneas.
- **Tarjeta de producto:** foto 1:1 en pozo gris con radio 12, una sola etiqueta (prioridad Nuevo > −X % > Bajo Cardmarket > Preventa), "desde" + precio, nombre del tipo y "N tiendas · colección". Rejilla de 2/3/4/5 columnas (0/600/900/1180 px).
- **Ficha (bottom sheet `<dialog>`):** asa, cabecera con foto 88 px, nombre, colección · idioma, mejor precio; nota si agrupa varios modelos; lista de ofertas (tienda, idioma, título de la tienda en 2 líneas, etiquetas, precio con anterior tachado, "Ir a la tienda"); pie con precio medio de Cardmarket y enlace a StockTCG. En ≥720 px es un diálogo centrado de 560 px. El botón atrás de Android la cierra.
- **Etiquetas:** 22 px de alto, radio 6, 12 px en 650.
- **Botones:** 34 px (42 px el grande), radio 9–10, acento sólido.
- **Iconos:** SVG propios en `<symbol>`, trazo 1.8, extremos redondeados. Sin emoji.
- **Estados:** esqueleto con latido al cargar, aviso ámbar sin conexión, vacíos que explican qué pasa (sin resultados → borrar búsqueda; sin stock → te avisaremos).

## Movimiento

Solo para indicar estado: la ficha sube en 240 ms (cubic-bezier .2,.8,.2,1), zoom de foto del 3,5 % al pasar el ratón, giro del icono de actualizar. Se desactiva con `prefers-reduced-motion`.
