# AGENTS.md - udesa-x-assistant-api

<!-- INICIO BLOQUE PROPIO - completado en cada servicio -->

Asistente por voz de UdeSA-X: un modelo de Bedrock elige, paso a paso, acciones de un catálogo cerrado sobre lo que el usuario ve en la app (ADR-023 de `udesa-x-platform`). Hoy tiene el bucle del agente y el arnés de evaluación del spike `AI-03`; el endpoint y el deploy llegan con `AI-04`.

## Stack y herramientas

- Lenguaje y runtime: Python 3.13 / FastAPI, Uvicorn
- Gestor de paquetes: uv
- Modelo: Amazon Bedrock con la API Converse (boto3), el ARN del perfil de inferencia del grupo en `BEDROCK_MODEL_ID`
- Persistencia: ninguna. El servicio no guarda el contenido de los pedidos.

## Checks y comandos

```bash
uv run ruff check .                                    # Lint
uv run ruff format --check .                           # Formato
uv run pytest --cov=src --cov-report=term              # Tests con un modelo falso: nunca llaman a Bedrock
uv run python -m evals.playground "dale like al tercero"   # Un pedido contra el modelo real
uv run python -m evals.run --label haiku-5.5               # Los 25 pedidos contra el modelo real
```

Los dos últimos cuestan y usan la cuota mensual del grupo: se corren a mano, nunca en el CI.

## Arquitectura y particularidades locales

- `src/assistant_api/agent/`: el catálogo de acciones (`catalog.py`), las instrucciones del modelo (`prompt.py`), el bucle (`loop.py`) y el cliente de Bedrock (`bedrock.py`). El bucle recibe el modelo y la app como parámetros, así los tests usan un modelo guionado y `AI-04` reusa el mismo bucle detrás del endpoint.
- `evals/`: la app simulada con 30 posts sintéticos (`app.py`, `feed.json`), los 25 pedidos con su estado final esperado (`requests.json`), la puntuación (`scoring.py`), el runner (`run.py`) y el playground (`playground.py`). Se testea en el CI, pero no entra en la imagen de producción.
- **Reglas de Bedrock de la cátedra:** siempre `maxTokens`, sin reintentos automáticos (el cliente sale con `total_max_attempts=1`), nunca access keys (local con `aws login --profile tds-group-3`, en el cluster con el rol del pod) y el ARN en una variable de entorno, nunca en el código.
- El texto de los posts es contenido de terceros: el prompt lo dice, y el catálogo no tiene acciones destructivas. Lo que otros pueden ver (retuitear, responder, publicar, seguir) lo confirma el usuario en la app.
- Datos de prueba siempre sintéticos: nunca contenido real de usuarios.

<!-- FIN BLOQUE PROPIO -->

<!-- INICIO BLOQUE COMUN - sincronizado desde udesa-x-platform, no editar la copia local -->

## Reglas del equipo

- **Ramas e issues**: Rama base `main`. Ramas de trabajo `feature-<nombre>` (funcionalidad), `fix-<nombre>` (defecto) o `chore-<nombre>` (mantenimiento y tooling, etiqueta `tech debt`), siempre asociadas a un issue en el mismo repositorio.
- **Idiomas**:
  - Código (`src/`, `tests/`), nombres de archivos, identificadores y comentarios en código: **inglés**.
  - Documentación (`docs/`, `README.md`), mensajes de commit y Pull Requests: **español**.
- **Commits**: Formato Conventional Commits (`feat:`, `fix:`, `docs:`, etc.) con descripción en español.
- **Simplicidad**: Soluciones mínimas y directas para el criterio de aceptación. No introducir librerías, patrones ni abstracciones nuevas sin un ADR aprobado en `docs/adr/`.

## Límites y flujo de trabajo del agente

- El agente inspecciona el repositorio (`git status`, `git diff`), edita archivos en el working tree, ejecuta checks locales y redacta propuestas de commit y PR.
- **El agente nunca commitea, pushea ni abre/aprueba/mergea Pull Requests.** La revisión y confirmación en Git la realiza siempre un integrante del equipo.
- **Sin firmas**: Nunca agregar `Co-Authored-By`, firmas o menciones del agente en commits, PRs ni código.

## Modo de planificación

- Planes extremadamente concisos: priorizar brevedad y concreción por sobre prosa formal.
- Al final de cada plan, incluir la lista de preguntas o dudas pendientes a resolver (si las hay).

## Skills (.agents/skills/)

- `explicar-implementacion`: Le pregunta al autor por qué tomó sus decisiones y con eso redacta la descripción del PR, una explicación breve en texto corrido para alguien sin el contexto de la pieza.
- `revisar-pr`: Guía paso a paso para la revisión técnica de Pull Requests.

<!-- FIN BLOQUE COMUN -->
