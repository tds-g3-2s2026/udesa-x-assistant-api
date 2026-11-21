# UdeSA-X Assistant API

Asistente por voz de UdeSA-X. El usuario pide algo hablando, la app lo pasa a texto en el
teléfono y un modelo de Amazon Bedrock elige, paso a paso, acciones de un catálogo cerrado
sobre lo que hay en pantalla: bajar en el feed, dar like, buscar, seguir, publicar. El modelo
nunca llama a la API de UdeSA-X: la app ejecuta cada acción como si el usuario tocara.

La decisión está en el ADR-023 de `udesa-x-platform`. La app llama a `POST /api/assistant/turns`
una vez por paso, y el servicio corre en el cluster desde el 16/11 (`AI-04`). El arnés de
evaluación del spike `AI-03` sigue en `evals/`.

Qué decide el modelo y qué decide la persona, los límites, los datos que salen del teléfono y
los sesgos están en [`docs/USO-RESPONSABLE.md`](./docs/USO-RESPONSABLE.md) (`AI-07`).

## Cómo funciona

1. Llega el pedido con la foto de la pantalla: los posts visibles como `p1`, `p2`... y las
   cuentas como `a1`, `a2`...
2. El modelo elige **una** acción del catálogo con tool use (API Converse).
3. La app la ejecuta y devuelve qué pasó y la pantalla nueva.
4. Se repite hasta que el modelo llama a `finish` con una respuesta corta, que la app lee en
   voz alta, o hasta llegar a 8 pasos.

| Acción | Qué hace | ¿La confirma el usuario? |
|---|---|---|
| `scroll_feed(direction)` | Baja o sube una pantalla | No |
| `refresh_feed()` | Vuelve al principio del feed | No |
| `open_post(ref)` | Abre el detalle de un post visible | No |
| `like_post(ref)` / `unlike_post(ref)` | Da o saca el like | No |
| `retweet_post(ref)` | Retuitea | Sí |
| `reply_to_post(ref, text)` | Responde con un texto que redacta el modelo | Sí |
| `compose_post(text)` | Publica un post que redacta el modelo | Sí |
| `search(query)` | Busca cuentas y posts | No |
| `open_profile(ref)` | Abre el perfil de un autor o de una cuenta | No |
| `follow(ref)` | Sigue a la cuenta del perfil abierto | Sí |
| `set_preference(name, value)` | Cambia el tema o el idioma del feed | No |
| `finish(say)` | Termina y responde | No |

Borrar, bloquear, denunciar, mandar mensajes y cambiar la contraseña no están en el catálogo:
el asistente puede llevar al usuario a esa pantalla, pero la acción la hace él.

Antes de llegar a la app, cada acción se valida: tiene que estar en el catálogo, con sus
parámetros, y un texto no puede pasar de 280 caracteres. Si el modelo llama a más de una
herramienta en el mismo paso, se ejecuta solo la primera.

## Estructura

```
src/assistant_api/
├── main.py            # la app de FastAPI: el endpoint, /healthcheck y la telemetría
├── turns.py           # POST /assistant/turns, con los límites de lo que manda la app
├── guards.py          # el interruptor, el JWT, el rate limit y los errores
├── telemetry.py       # trazas, logs y métricas, sin el texto de nadie
├── settings.py        # la configuración que se lee del entorno
└── agent/
    ├── catalog.py     # las acciones como herramientas de Converse y su validación
    ├── prompt.py      # las instrucciones del modelo
    ├── loop.py        # el bucle: el modelo elige, la app ejecuta, hasta finish o 8 pasos
    ├── turn.py        # un paso del endpoint, armado de nuevo con lo que manda la app
    └── bedrock.py     # el cliente de Bedrock, con tope de tokens y sin reintentos
evals/
├── feed.json          # 30 posts y 12 cuentas sintéticos
├── app.py             # la app simulada: 5 posts por pantalla, ejecuta el catálogo
├── requests.json      # los 30 pedidos, cada uno con el estado final esperado
├── scoring.py         # compara el estado final con el esperado
├── run.py             # corre los pedidos contra el modelo y mide
└── playground.py      # un pedido, paso a paso
```

## Tests

```bash
uv sync
uv run pytest --cov=src --cov-report=term
```

Usan un modelo guionado: no llaman a Bedrock, no cuestan y corren en el CI.

## Evaluar un modelo

La evaluación corre contra Bedrock de verdad: **cuesta y usa la cuota mensual del grupo**, así
que se corre a mano y nunca en el CI.

1. Sesión de AWS, con la CLI 2.32 o más nueva. Pide el código de MFA y no deja access keys:

   ```bash
   aws login --remote --profile tds-group-3
   ```

2. Las variables. El ARN es el del perfil de inferencia local del grupo, que está en la guía
   de Bedrock de la cátedra. No se escribe en el repo.

   ```bash
   export AWS_PROFILE=tds-group-3
   export BEDROCK_REGION=us-east-1
   export BEDROCK_MODEL_ID=<ARN del perfil local>
   ```

   En PowerShell: `$env:AWS_PROFILE = "tds-group-3"`, y lo mismo con las otras dos.

3. Un pedido, para probar un cambio en el prompt o en el catálogo:

   ```bash
   uv run python -m evals.playground "dale like al último post de Tomi"
   ```

   Muestra cada paso con la acción, los tokens y la demora, y al final qué cambió en la app.

4. Los 30 pedidos:

   ```bash
   uv run python -m evals.run --label haiku-5.5
   uv run python -m evals.run --label haiku-5.5 --only R01,R06   # algunos
   ```

   Imprime cada pedido y un resumen: tasa de éxito, en total y por tipo de pedido, llamadas
   al modelo por pedido, demora hasta la primera acción (mediana y p95), largo de la
   respuesta hablada, tokens, costo y si el modelo obedeció a alguno de los dos posts que
   intentan darle órdenes. El detalle queda en `evals/results/`.

5. **Contra el servicio desplegado**, el camino completo que recorre el teléfono: el JWT, el
   rate limit, el prompt de producción y Bedrock con el rol del pod. La app sigue siendo la
   simulada. No necesita sesión de AWS, sino una cuenta de la app:

   ```bash
   uv run python -m evals.run --label produccion --remote https://tds-group-3.tds-linar.udesa.edu.ar/api
   ```

   Pide el email o handle y la contraseña, o los toma de `EVAL_IDENTIFIER` y `EVAL_PASSWORD`.
   Son unos 70 pasos, debajo del límite de 160 por hora de una cuenta. El endpoint no informa
   tokens, así que el costo sale de una corrida local. Además cuenta las acciones que ven
   otros y que el servidor no marcó para confirmar: tienen que ser cero.

`--thinking` prende el razonamiento del modelo. Está apagado por defecto: para elegir una de
doce acciones no hace falta, y demora segundos la primera acción.

Un pedido sale bien cuando el modelo terminó con `finish` y la app quedó como se esperaba. Se
compara el estado final y no el camino: buscar en lugar de bajar en el feed también vale. Los
textos que redacta el modelo se comparan por palabras clave.

## Reglas para usar Bedrock

Son las de la guía de la cátedra:

- Toda llamada lleva `maxTokens` (400).
- Sin reintentos automáticos: el cliente sale con un solo intento, y un error se ve en lugar de
  convertirse en varias llamadas contra la cuota.
- Tiempo límite: 3 segundos para conectar y 10 para la respuesta. Si Bedrock no contesta, rechaza
  la llamada o no se puede conectar, el endpoint responde `503` con `model-unavailable`, y el
  motivo queda en el log sin el texto del pedido.
- Nunca access keys: en local con `aws login`, en el cluster con el rol del pod.
- El ARN del modelo va en una variable de entorno, nunca en el código.

## Despliegue

Corre en `tds-cluster`, namespace `tds-group-3`, con el pipeline de platform: cada push a `main`
que pasa el CI construye la imagen, la publica en ECR (`tds-group-3/assistant-api`) y aplica
`k8s/`. No tiene base, así que no hay migración.

- **Bedrock sin claves.** El pod corre con la ServiceAccount `tds-group-3-bedrock-sa`, que
  aplica el docente desde `k8s/serviceaccount.yaml`: el pipeline no puede crear
  ServiceAccounts y nunca aplica ese archivo. EKS lee su anotación y le da al pod
  credenciales temporales del rol `tds-group-3-bedrock-role`, el único que acepta esa
  ServiceAccount.
- **Configuración** en `k8s/configmap.yaml`: la región del perfil (`us-east-2`), el interruptor
  `ASSISTANT_ENABLED` y el límite `TURNS_PER_HOUR`.
- **Secrets del repositorio**, que el pipeline arma como `assistant-api-secret`:

| Secret | Valor |
|---|---|
| `BEDROCK_MODEL_ID` | El ARN del perfil de inferencia de EKS del grupo. Nunca en el repo |
| `REDIS_URL` | El Redis del cluster, base `/2` (users-api usa `/0` y posts-api `/1`) |
| `JWT_PUBLIC_KEY` | La pública de users-api, la misma que tiene posts-api |
| `OTEL_EXPORTER_OTLP_HEADERS` | De organización: no hace falta cargarlo |

Para apagar el asistente sin tocar el código, `ASSISTANT_ENABLED: "false"` en el ConfigMap:
el pipeline reemplaza los pods cuando cambia la configuración y el endpoint responde `503`.

## Métricas y trazas

Igual que los demás servicios (ADR-013): un span por request, cada línea de log con su traza y
la duración de los requests en segundos. Salen a Grafana Cloud solo si está
`OTEL_EXPORTER_OTLP_ENDPOINT`, que en el cluster viene del ConfigMap. **Ninguna lleva el texto
del pedido, de los posts ni de la respuesta** (ADR-023): solo nombres del catálogo y números.

| Métrica | Qué mide | Atributos |
|---|---|---|
| `assistant.steps` | Pasos respondidos | `action`, del catálogo o `finish`, y `outcome`: `action`, `finished`, `no_action`, `refused` o `step_limit` |
| `assistant.model.duration` | Cuánto tardó cada llamada al modelo, en segundos | |
| `assistant.model.tokens` | Tokens que leyó y escribió el modelo | `type`: `input` u `output` |
| `assistant.spoken.length` | Largo de la oración que la app lee al terminar, en caracteres | |

Cada llamada al modelo es un span `assistant.model`, hijo del span del request, con los tokens
como atributos. En Grafana las métricas aparecen con guiones bajos y el sufijo de Prometheus,
como `assistant_steps_total`.
