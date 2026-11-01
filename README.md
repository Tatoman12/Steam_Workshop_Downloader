# Steam Workshop Downloader

App web local (una sola ventana) para bajar mods de Steam Workshop: pegas el enlace,
eliges la carpeta y pulsas **Descargar**.

## Cómo usarla

1. Doble clic en **`iniciar.bat`**.
2. Se abre el navegador en `http://127.0.0.1:8756`.
3. Pega el enlace del mod (o de una colección) y elige la carpeta.
4. Pulsa **Descargar**.

La primera vez descarga **DepotDownloader** (unos 30 MB y un único `.exe`, tarda
menos de un minuto). Después ya es rápido.

Alternativa por consola:

```bat
python -m pip install -r requirements.txt
python app.py
```

## Explorar el Workshop

En la pestaña **Explorar Workshop** puedes buscar un juego por su nombre y ver
sus mods sin salir de la app:

- **Buscador de mods**: escribe el nombre de un mod para filtrar los resultados
  del juego, o pega su ID/URL para abrirlo directamente. Los resultados también
  se pueden ordenar y paginar.
- **Más populares**: ordenados por estrellas (mejor valorados).
- **Más suscripciones**: ordenados por número de suscriptores.
- **Paginación**: navega con los botones de página (primera, última y las
  cercanas a la actual) en lugar de cargar la lista de golpe.

Cada tarjeta muestra miniatura, título, descripción corta, valoración,
suscriptores, tamaño y etiquetas. El botón **Descargar** lleva el mod a la
pestaña de descarga y lo baja a la carpeta que tengas elegida; **↗** abre su
página en Steam. También se guardan los últimos juegos visitados.

No hace falta ninguna clave de API: la app lee los datos públicos que Steam
publica en la propia web del Workshop. Los resultados se cachean unos minutos
para no pedir la misma página dos veces.

## Iniciar sesión con Steam (opcional)

La mayoría de mods se descargan de forma anónima. Si el mod es de un juego de pago,
pulsa el botón **Steam** de la cabecera (muestra el estado de la cuenta) y elige
**Conectar con Steam**:

1. Aparece un **código QR** en el desplegable de la cabecera.
2. Abre **Steam** en el móvil → **Steam Guard → Iniciar sesión con QR**.
3. Apunta la cámara al código.

La sesión queda guardada en tu PC (token de Steam, no la contraseña), así que las
siguientes descargas ya no piden nada. **Tu contraseña nunca pasa por esta app**: la
autorización se hace desde la app oficial de Steam. Puedes desconectar la cuenta
cuando quieras con **Desconectar**.

Si no tienes la app móvil de Steam, los mods de juegos de pago no se pueden bajar
(limitación de Steam, no de la app).

## Qué hace por dentro

- Resuelve el enlace con la API pública de Steam (`GetPublishedFileDetails`) para
  obtener título, app y tamaño.
- Descarga el contenido con **DepotDownloader** (SteamKit2, la misma red oficial de
  Steam). La descarga se hace en una carpeta temporal oculta y solo se mueve a su
  destino cuando está completa, así que un fallo no borra una copia anterior.
  También extrae solo los archivos reales del mod (`.bin` de contenido antiguo).
- Permite iniciar sesión escaneando un QR con la app oficial de Steam y reutiliza
  ese token en las siguientes descargas.
- Navega por el Workshop de cualquier juego (populares, más suscritos, búsqueda
  por nombre o ID y paginación) leyendo los datos públicos de la web de Steam,
  sin clave de API.

## Notas

- Funciona de forma anónima con la mayoría de juegos. Si un juego es de pago y exige
  cuenta, la app te avisa y te ofrece **Conectar con Steam y reintentar**.
- El nombre de carpeta puede ser el título del mod o su ID (`...\workshop\content\<appid>`
  si quieres el formato que usa Steam).
- Solo para uso personal: descarga mods que tengas derecho a usar.

## Archivos

| Archivo | Qué es |
| --- | --- |
| `app.py` | Servidor, descargas, login con QR y explorador del Workshop |
| `static/index.html` | Interfaz |
| `iniciar.bat` | Lanzador para Windows |
| `config.json` | Carpeta de destino y preferencias (local, no se versiona) |
| `vendor/depotdownloader/` | DepotDownloader (se descarga solo) |
| `tests/` | Tests básicos (`python -m unittest discover -s tests`) |
