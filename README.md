# StockTCG alertas

Avisos en el móvil cuando vuelve a haber stock en [stocktcg.net](https://stocktcg.net) de los
productos que te interesan.

cron-job.org lanza `monitor.py` en GitHub Actions cada 5 minutos (y GitHub lo ejecuta cada 30 como respaldo). El script descarga el catálogo de StockTCG,
aplica tus filtros de `filtros.toml` y, si un producto que cumple algún filtro vuelve a estar
disponible, manda una notificación al móvil a través de [ntfy](https://ntfy.sh).

📱 **Web-app: https://ybanin.github.io/stocktcg-alertas/**. Todo lo que hay en stock ahora mismo
según tus filtros, con fotos, precios y enlace directo a cada tienda. En Chrome para Android:
menú ⋮ → **Instalar aplicación** (o **Añadir a pantalla de inicio**) y se abre como una app.
La web está en `docs/` y los datos que muestra en `datos/stock.json`, que se actualizan solos.

## Cambiar lo que te avisa

Desde la app: **Ajustes → Mis avisos** (necesita la clave de GitHub de la app). Se guardan en
`stocktcg-favoritos/filtros.json` y se aplican en la siguiente comprobación. Las opciones generales
(idioma por defecto, bajadas, chollos, preventas en español) siguen en [`filtros.toml`](filtros.toml).

## Pedir un resumen del stock actual

Pestaña **Actions** → **Vigilar stock** → **Run workflow** → **Run workflow**.
Te llega una notificación por cada filtro con todo lo que hay en stock ahora mismo.
También llega automáticamente cada vez que cambias `filtros.toml`.

## Pausar los avisos

Pestaña **Actions** → **Vigilar stock** → menú `···` → **Disable workflow** (y **Enable** para volver).

## Notas

- Solo avisa de **reposiciones**: productos que no estaban disponibles en la comprobación anterior.
  Lo que ya estaba en stock lo tienes en el resumen y en la web-app.
- Si el repositorio pasa 60 días sin cambios, GitHub pausa las ejecuciones programadas y te avisa
  por email. Basta con volver a activarlo en Actions.
- Los precios en otras monedas (DKK, SEK, CHF…) se convierten a euros de forma aproximada.
