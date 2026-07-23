# Cookies de YouTube

Coloca en esta carpeta un archivo `cookies.txt` en formato Netscape con las
cookies de una cuenta de YouTube. Esto evita los bloqueos del tipo
"Sign in to confirm you're not a bot" cuando el servidor hace muchas descargas.

## Como exportarlo

1. Abre una ventana de incognito en Chrome o Firefox e inicia sesion en YouTube
   con una cuenta secundaria (no uses tu cuenta principal).
2. Instala la extension "Get cookies.txt LOCALLY".
3. Con `youtube.com` abierto, exporta las cookies y guarda el archivo como
   `cookies.txt` dentro de esta carpeta.
4. Cierra la ventana de incognito SIN cerrar sesion, para que las cookies sigan
   siendo validas.
5. Reinicia el contenedor:

   ```
   docker compose restart
   ```

El backend detecta el archivo automaticamente. Comprueba en `/health` que el
campo `cookies` sea `true`.

Este archivo contiene credenciales y no debe subirse al repositorio.
