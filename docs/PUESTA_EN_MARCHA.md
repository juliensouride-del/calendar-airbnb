# Puesta en marcha de las respuestas a los huéspedes

Lista para dejar funcionando los borradores en Telegram. El calendario no se toca: si algo de esto falla,
la sincronización sigue igual.

Modo **copiar y pegar**: el bot no tiene ninguna contraseña de correo. Cada borrador llega a Telegram con el
texto listo; lo copias, lo pegas en la conversación de Airbnb y tocas «✅ Ya lo pegué». Para que el bot envíe
solo, mira el final.

> Tokens y contraseñas van solo en *GitHub → Settings → Secrets* y en *Vercel → Environment Variables*;
> nunca en un chat, en un issue ni en el código.

## 1. Bot de Telegram

- [ ] En Telegram, **@BotFather** → `/newbot` → un nombre y un usuario acabado en `bot` → guarda el **token**.

## 2. Secreto compartido

- [ ] Genera una cadena aleatoria (es la misma en GitHub y en Vercel):

      python -c "import secrets; print(secrets.token_hex(32))"

## 3. Vercel

- [ ] Proyecto con el contenido de la carpeta `bot/` (Vercel lo detecta como app Flask; `bot/vercel.json` fija la región `fra1`).
- [ ] *Storage → Create → Upstash for Redis* (gratis, región UE) → *Connect* al proyecto
      (crea solas `KV_REST_API_URL` y `KV_REST_API_TOKEN`).
- [ ] *Settings → Environment Variables* (Production):

  | Variable | Valor |
  | --- | --- |
  | `TELEGRAM_TOKEN` | el token del paso 1 |
  | `BANDEJA_SECRETO` | la cadena del paso 2 |
  | `URL_BASE` | `https://<dominio de producción>` (lo usan el webhook y el botón ✏️ Editar) |
  | `APROBADORES` | tu ID de Telegram (paso 5) |

- [ ] Comprueba que `https://<dominio>/api/salud` responde `{"ok": true}`.

## 4. GitHub → *Settings → Secrets and variables → Actions*

| Secreto | Valor |
| --- | --- |
| `BANDEJA_URL` | `https://<dominio de producción>` (sin `/` al final) |
| `BANDEJA_SECRETO` | la cadena del paso 2 |
| `BASE_PRIVADA` | **todo** el contenido del archivo local `base_privada.toml` (no está en el repositorio) |

Desde la terminal, en la carpeta del repositorio (`gh` pide el valor y no lo muestra):

    gh secret set BANDEJA_SECRETO --repo juliensouride-del/calendar-airbnb
    gh secret set BASE_PRIVADA --repo juliensouride-del/calendar-airbnb < base_privada.toml

Los secretos de Yahoo, Google Calendar y Gemini ya están y no se tocan.

## 5. Comprobar y autorizarte

- [ ] *Actions → Airbnb → Calendar → Run workflow* → rama del PR (o `main`) → modo **`comprobar`**.
      Configura el webhook de Telegram.
- [ ] Abre tu bot en Telegram y escribe `/start`: te dice tu **ID**.
- [ ] Ponlo en `APROBADORES` en Vercel → *Redeploy* → lanza **`comprobar`** otra vez.
- [ ] Te llega un **borrador de demostración 🧪**: prueba ✏️ Editar, ✅ Enviar y ❌ Descartar. No se envía nada a nadie.

## 6. Fusionar

- [ ] Fusiona el PR en `main`: las ejecuciones programadas solo corren en `main`.

## Más adelante: que el bot envíe solo (Yahoo del anfitrión)

1. Vercel: `YAHOO_USUARIO` y `YAHOO_CLAVE_APP` del anfitrión (los mismos valores que en GitHub) → *Redeploy*.
2. Con el primer borrador real: **✅ Enviar → ✅ Sí** y comprueba en Airbnb que aparece en la conversación.
3. `APROBADORES` = el ID de Telegram de quien vaya a aprobar (escribe `/start` en el bot para verlo).
4. Lo ideal: el propietario pone el repositorio en **privado** y pasa el proyecto de Vercel a su cuenta.

## Pausarlo o quitarlo

- **Pausar:** borra el secreto `BANDEJA_URL`. El calendario sigue igual.
- **Quitarlo del todo:** README, sección 6 → «Apagarlo o quitarlo».
