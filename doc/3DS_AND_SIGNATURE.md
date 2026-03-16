# Revisión flujo 3DS y firma – Paycomet JET

## 1. Flujo 3DS (resumen)

### Lo que hace el módulo

1. **Pago con token JET**  
   El usuario introduce los datos en los iframes de Paycomet; se obtiene un `jetToken` y se llama a `POST /v1/payments` con ese token.

2. **Respuesta con 3DS**  
   Si la API devuelve `errorCode == 0` y `challengeUrl`:
   - La transacción se pone en estado **pending** con mensaje "Esperando autenticación 3D Secure".
   - Se devuelve al front `{ status: '3ds_required', challenge_url: ... }`.
   - El front redirige al usuario a `challenge_url` (página del banco para 3DS).

3. **Retorno desde 3DS**  
   Tras completar 3DS, Paycomet redirige al usuario a las URLs que nosotros enviamos en el pago:
   - `urlOk` → `/payment/jetframe/return?reference=...&status=ok`
   - `urlKo` → `/payment/jetframe/return?reference=...&status=ko`

4. **Procesamiento del retorno**  
   El controlador `jetframe_return` recibe GET o POST, obtiene `reference` y `status`, y pasa **todos** los parámetros recibidos a `_handle_notification_data` → `_process_notification_data`:
   - Si `status == 'ok'` y la transacción no está ya `done` → `_set_done(...)`.
   - Si `status == 'ko'` y la transacción no está `done` ni `error` → `_set_error(...)`.
   - Se redirige al usuario a `/payment/status`.

### Comprobaciones realizadas

- **Estados**: `_set_pending` para “esperando 3DS” y luego `_set_done` / `_set_error` en el return están alineados con los estados permitidos en Odoo (`pending` → `done` está permitido).
- **Idempotencia**: Si el usuario recarga la URL de retorno, `_set_done`/`_set_error` no cambian el estado si ya está en `done` o `error`.
- **Logging**: En el return se registra `full_data` (todos los parámetros) para poder ver si Paycomet añade campos extra (p. ej. firma).

---

## 2. Firma / autenticación

### Request a la API REST (ejecutar pago)

- **Implementación actual**: Solo se envía el header **`PAYCOMET-API-TOKEN`** con el valor del campo API Key del proveedor. No se envía ninguna firma en el body.
- **Documentación Paycomet**: Hay que contrastar en la documentación oficial de la API REST (ej. “Ejecutar pago”, “Autenticación”) si:
  - Solo exigen ese header (nuestra implementación sería correcta).
  - O si además exigen una **firma en el body** (p. ej. hash de order + amount + secret). En ese caso debe implementarse en `payment_transaction.py` dentro de `_jetframe_get_form_challenge_url`, donde se construye el `payload` y los `headers` de `/v1/form`.

### Retorno 3DS (urlOk / urlKo)

- **Implementación actual**: No se verifica ninguna firma en la URL de retorno. Se confía en `reference` + `status` y en que la URL la hemos construido nosotros.
- **Riesgo**: Si Paycomet documenta que en el retorno envían un parámetro de firma (p. ej. `signature`, `hash`, `DS_SIGNATURE`) para que el comercio verifique que la redirección es legítima, **hay que implementar esa verificación**.
- **Dónde**: En `models/payment_transaction.py`, método **`_verify_paycomet_return_signature(self, notification_data)`**. Ahora mismo devuelve siempre `True` y tiene un TODO. Cuando tengas la documentación de Paycomet:
  1. Comprueba qué parámetros envían en el retorno (en los logs verás `full_data` en cada vuelta del 3DS).
  2. Implementa el cálculo de la firma esperada según la documentación (normalmente orden + importe + clave secreta, y comparar con el parámetro que envían).
  3. Si no coincide, devolver `False` (el módulo ya marcará la transacción en error y registrará el intento en log).

---

## 3. Qué revisar en la documentación Paycomet

- **API REST – Autenticación**: Si además de `PAYCOMET-API-TOKEN` exigen firma en el body y su algoritmo (campos a concatenar, orden, codificación, HMAC/SHA256, etc.).
- **API REST – Ejecutar pago (JET)**: Nombres exactos de campos (`challengeUrl`, `errorCode`, `urlOk`, `urlKo`, etc.) y si hay campos adicionales obligatorios para 3DS.
- **Retorno 3DS (urlOk/urlKo)**: Si en la redirección envían parámetros extra y, en particular, si exigen **verificación de firma** en el retorno; si es así, algoritmo y parámetros a usar.

Con los cambios hechos en el código (logging de `full_data` en el return y método `_verify_paycomet_return_signature` listo para implementar), puedes comprobar en logs qué envía Paycomet realmente y adaptar la firma cuando tengas la documentación exacta.

---

## 4. Guía práctica: revisar documentación, prueba 3DS e implementar firma

### 4.1 Dónde conseguir la documentación oficial de Paycomet

- **Portal de documentación**: https://docs.paycomet.com (puede cargar contenido por JavaScript; si no ves el contenido, prueba en otro navegador o descarga PDF si lo ofrecen).
- **Back office / área de comercio**: Muchos proveedores incluyen “Documentación API” o “Guía de integración” en el panel del comercio.
- **Soporte Paycomet**: Solicitar a tu gestor o a soporte la “Documentación API REST” y la sección sobre “Retorno 3D Secure” o “URLs de retorno (urlOk/urlKo)”.
- **Buscar en la documentación**:
  - **API de ejecución de pago**: si exigen además del header `PAYCOMET-API-TOKEN` algún campo de **firma en el body** (nombre del campo y algoritmo: p. ej. SHA256, HMAC-SHA256, y qué campos concatenar y en qué orden).
  - **Retorno 3DS**: si en la redirección a urlOk/urlKo envían un parámetro de **firma/hash** y cómo debe calcularse la firma para verificarla (campos a usar, orden, codificación, clave secreta).

### 4.2 Hacer una prueba 3DS y revisar los logs del return

1. **Activar registro de peticiones HTTP (opcional)**  
   En Odoo, asegúrate de que el nivel de log del módulo sea al menos INFO (en `payment_transaction` y el controlador se usa `_logger.info` con `full_data`).

2. **Realizar un pago con 3DS**  
   Usa una tarjeta de prueba que dispare 3D Secure (Paycomet suele indicar en su documentación qué BINs o tarjetas de test fuerzan 3DS). Completa el flujo hasta que el navegador vuelva a tu tienda (urlOk o urlKo).

3. **Revisar los logs**  
   Busca en los logs la línea:
   - `"Paycomet 3DS return – reference=... status=... full_data=..."`
   - El valor de `full_data` es el diccionario completo de parámetros que recibió el return (GET o POST). Ahí verás si Paycomet envía además de `reference` y `status` algún campo como `signature`, `hash`, `DS_SIGNATURE`, `order`, `amount`, etc.

4. **Comparar con la documentación**  
   Contrasta los nombres y valores de `full_data` con lo que indica la documentación de Paycomet para el retorno 3DS (parámetros que envían y, si aplica, cómo verificar la firma).

### 4.3 Si la documentación exige firma en el return: implementarla

Implementa la verificación en **`models/payment_transaction.py`**, método **`_verify_paycomet_return_signature(self, notification_data)`**.

- **Entrada**: `notification_data` es el diccionario completo de parámetros recibidos en el return (lo que ves en `full_data` en los logs).
- **Salida**: devolver `True` si la firma es correcta o no es exigida; `False` si la verificación falla.

**Ejemplo de patrón típico** (solo si la documentación de Paycomet lo indica así; ajusta nombres de parámetros y algoritmo al documento oficial):

```python
import hmac
import hashlib

def _verify_paycomet_return_signature(self, notification_data):
    # 1) Si Paycomet no envía firma en el return, no verificar nada
    received_signature = notification_data.get('signature')  # o el nombre que indique la doc
    if not received_signature:
        return True  # o False si la doc exige siempre firma

    # 2) Obtener la clave secreta (según doc: API Key, o clave específica de firma)
    secret = (self.provider_id.paycomet_api_key or '').encode('utf-8')

    # 3) Construir la cadena a firmar en el orden que indique la documentación
    # Ejemplo: orden + amount + reference + ...
    order = notification_data.get('order', '')
    amount = notification_data.get('amount', '')
    ref = notification_data.get('reference', '')
    string_to_sign = f"{order}{amount}{ref}"  # ¡Ajustar al orden exacto de la doc!

    # 4) Calcular la firma (ejemplo HMAC-SHA256; puede ser SHA256 simple según doc)
    expected = hmac.new(secret, string_to_sign.encode('utf-8'), hashlib.sha256).hexdigest()

    # 5) Comparación segura
    return hmac.compare_digest(expected.lower(), (received_signature or '').lower())
```

- Sustituye `'signature'`, `'order'`, `'amount'` por los **nombres exactos** que use Paycomet en el return.
- Ajusta el **orden y contenido** de `string_to_sign` a lo que diga la documentación (a veces incluyen separadores, codificación Base64, etc.).
- Si la documentación indica **SHA256** sin HMAC, usa `hashlib.sha256((string_to_sign + secret).encode()).hexdigest()` o la variante que indiquen.

### 4.4 Error Paycomet [1276]: No ha sido posible generar un token de operación

Este mensaje lo envía el **iframe de Paycomet** (JavaScript) en el navegador cuando no puede generar el token JET. No proviene de los tests unitarios.

**Causas habituales:**

1. **La página se sirve por HTTP** – Paycomet exige **HTTPS** para generar el token. En HTTP el iframe devuelve 1276.
2. **JET ID de prueba** – Si el JET ID es tipo `TEST_JET_ID` o empieza por `TEST_`, Paycomet puede rechazar la generación. Para pagos reales hay que usar el **JET ID real** del back office de Paycomet.
3. **Credenciales incorrectas o caducadas** – Revisar en Contabilidad → Configuración → Proveedores de pago que Merchant Code, Terminal ID, JET ID y API Key sean correctos y vigentes.

En el formulario de pago, cuando ocurre el 1276, se muestra un mensaje más claro con estas comprobaciones. En producción, usar siempre **HTTPS** y **credenciales y JET ID reales**.

### 4.5 Si la documentación exige firma en el body de la API (ejecutar pago)

Si en la documentación de la API REST (ejecutar pago) indican que además del header `PAYCOMET-API-TOKEN` hay que enviar un campo de **firma en el body** (p. ej. `signature`, `hash`):

1. Abre **`models/payment_transaction.py`** y localiza el bloque donde se construye `payload` y `headers` en **`_jetframe_get_form_challenge_url`**.
2. Según la documentación, calcula la firma con el algoritmo indicado (típicamente un hash de: merchant code + terminal + order + amount + currency + secret, en el orden que indiquen).
3. Añade el campo al `payload` antes de hacer el `requests.post`, por ejemplo:
   - `payload['signature'] = computed_signature`  
   o el nombre exacto que indique la documentación (p. ej. `payload['payment']['signature']` si debe ir dentro de `payment`).
4. La clave secreta suele ser la API Key o una “clave de firma” que te proporcione Paycomet; si es distinta a la API Key, puedes añadir un campo nuevo en el proveedor (p. ej. `paycomet_secret_key`) y usarlo solo para la firma.
