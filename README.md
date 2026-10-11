# StockTCG alertas

Avisos en el móvil cuando vuelve a haber stock en [stocktcg.net](https://stocktcg.net) de los
productos que te interesan.

GitHub Actions ejecuta `monitor.py` cada ~5 minutos. El script descarga el catálogo de StockTCG,
aplica tus filtros de `filtros.toml` y, si un producto que cumple algún filtro vuelve a estar
disponible, manda una notificación al móvil a través de [ntfy](https://ntfy.sh).

## Cambiar lo que te avisa

Edita [`filtros.toml`](filtros.toml). Puedes hacerlo desde la propia web de GitHub (icono del lápiz)
o desde la app de GitHub en el móvil. Los cambios se aplican en la siguiente comprobación.

## Probar los filtros

Pestaña **Actions** → **Vigilar stock** → **Run workflow** → marca *Modo prueba* → **Run workflow**.
Te llega una notificación de prueba y, en el registro de la ejecución, ves la lista de productos
que ahora mismo están en stock y cumplen tus filtros.

## Pausar los avisos

Pestaña **Actions** → **Vigilar stock** → menú `···` → **Disable workflow** (y **Enable** para volver).

## Notas

- Solo avisa de **reposiciones**: productos que no estaban disponibles en la comprobación anterior.
  Al cambiar los filtros no te llegan avisos de lo que ya estaba en stock.
- Si el repositorio pasa 60 días sin cambios, GitHub pausa las ejecuciones programadas y te avisa
  por email. Basta con volver a activarlo en Actions.
- Los precios en otras monedas (DKK, SEK, CHF…) se convierten a euros de forma aproximada.
