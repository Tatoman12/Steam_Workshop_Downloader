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

## Iniciar sesión con Steam (opcional)

La mayoría de mods se descargan de forma anónima. Si el mod es de un juego de pago,
pulsa **Conectar con Steam**:

1. Aparece un **código QR** en la app.
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
  Steam), sin copias temporales: los archivos van directo a la carpeta elegida.
  También extrae solo los archivos reales del mod (`.bin` de contenido antiguo).
- Permite iniciar sesión escaneando un QR con la app oficial de Steam y reutiliza
  ese token en las siguientes descargas.

## Notas

- Funciona de forma anónima con la mayoría de juegos. Si un juego es de pago y exige
  cuenta, la app te avisa y te ofrece **Conectar con Steam y reintentar**.
- El nombre de carpeta puede ser el título del mod o su ID (`...\workshop\content\<appid>`
  si quieres el formato que usa Steam).
- Solo para uso personal: descarga mods que tengas derecho a usar.

## Archivos

| Archivo | Qué es |
| --- | --- |
| `app.py` | Servidor, trabajos de descarga y login con QR |
| `static/index.html` | Interfaz |
| `iniciar.bat` | Lanzador para Windows |
| `config.json` | Carpeta de destino y preferencias (se crea solo) |
| `vendor/depotdownloader/` | DepotDownloader (se crea solo) |
