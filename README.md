# sdd-codex

Spec-driven task runner: читает задачи из структурированных markdown-спецификаций
(формат spec-runner) и исполняет их, запуская кодинг-агентов OpenAI как
CLI-подпроцессы `codex`. Центральная идея — работа агента проверяема и
неподделываема: красный тест доказан самим runner'ом, менять можно только
разрешённые файлы, диф проходит ревью до коммита.

## Требования

- [uv](https://docs.astral.sh/uv/) — и для самого sdd-codex, и в целевом проекте
- `codex` CLI в PATH, авторизованный (`codex login`; подходит и ChatGPT-аккаунт,
  и `OPENAI_API_KEY` в окружении)
- git-репозиторий в целевом проекте

Проверить всё разом: `sdd-codex doctor`.

## Установка

```sh
# из каталога этого репозитория
uv tool install .          # команда sdd-codex появляется в PATH
# или без установки
uv run sdd-codex --help
```

## Быстрый старт: полный цикл

```sh
mkdir myapp && cd myapp
git init -b main
uv init --name myapp && uv add --dev pytest
printf '.sdd-codex/\n.venv/\n__pycache__/\n' > .gitignore
git add -A && git commit -m "initial"

# 1. Планировщик создаёт спеку из описания (и выходит, ничего не исполняя)
sdd-codex spec "CLI-утилита wordcount: принимает путь к файлу и печатает число слов"

# 2. Проверка формата — бесплатно, без вызовов API
sdd-codex validate
git add -A && git commit -m "spec"

# 3. Исполнение задач
sdd-codex run
```

По ходу прогона в терминале видно каждую задачу и её стадии; по завершении код
закоммичен в текущую ветку, задачи в `spec/tasks.md` помечены `DONE`, а в
`.sdd-codex/runs/<timestamp>/` лежат отчёты и логи.

## Команды

| Команда | Что делает |
| --- | --- |
| `sdd-codex spec "<описание>"` / `--from-file path.md` | агент-планировщик создаёт `spec/requirements.md`, `spec/design.md`, `spec/tasks.md` и выходит |
| `sdd-codex run [--task TASK-003]` | исполняет задачи из готового набора (все или одну) |
| `sdd-codex validate` | парсит набор и печатает ошибки формата (`файл:строка`), без вызовов API |
| `sdd-codex doctor` | проверяет `codex` в PATH, ключ/сессию, git-репозиторий, конфиг |

У всех команд есть `--config path` — путь к конфиг-файлу.

Создание спеки и запуск нарочно разделены: общего состояния нет, связь только
через файлы на диске. Поэтому готовый набор от другого приложения (в формате
spec-runner) исполняется так же — положите файлы в `spec/` и запустите
`sdd-codex run`.

## Как исполняется задача

Для каждой задачи runner проходит цикл:

1. **написание теста** — агент-кодер пишет тест по спеке задачи;
2. **красный прогон** — runner *сам* выполняет команду проверки и требует
   падения; тест, прошедший до реализации, закрывает попытку как провальную.
   Вывод падения сохраняется; тестовые файлы замораживаются по хешу;
3. **кодинг** — агент пишет реализацию (тесты менять уже нельзя);
4. **зелёный прогон** — та же команда должна пройти;
5. **заморозка** — изменены могут быть только файлы из `**Touches:**` задачи;
   любой лишний файл — попытка отклонена и откачена;
6. **ревью** — отдельный агент смотрит диф и выносит вердикт; замечания
   передаются в следующую попытку;
7. **коммит** — в текущую ветку, без ветвления и PR; отметка `DONE` ставится
   в самом `tasks.md`.

Неудачные попытки повторяются до `run.max_retries`; исчерпание лимита
останавливает прогон.

## Формат задач (коротко)

```markdown
### TASK-001: Название
P2 | TODO   Est: 0.5d

Описание.

**Checklist:**
- [ ] шаг

**Depends on:** [TASK-000]
**Traces to:** [REQ-001], [DESIGN-001]
**Touches:** pyproject.toml, src/app.py, tests/test_app.py
**Verifies:** tests/test_app.py::test_behaviour
```

Проверка задачи задаётся одним из двух полей:

- `**Verifies:**` — pytest-селекторы; runner подставляет их в
  `verify.selector_runner` (по умолчанию `uv run pytest {selectors} -q`);
- `**Test:**` — полная shell-команда (в uv-проекте — `uv run pytest ...`).

Задача без одного из этих полей не исполняется. Значения полей исполняются
буквально — без markdown-бэктиков (обрамляющие бэктики парсер снимает сам).

## Конфигурация

Один файл `sdd-codex.config.yaml` в корне целевого репозитория. Он необязателен —
дефолты рабочие. Вендор не настраивается: OpenAI зашит в код; неизвестный ключ
в конфиге — ошибка старта.

```yaml
openai:
  models:                  # модель отдельно на каждую роль
    planner: gpt-6.1-sol
    coder: gpt-6.1-sol
    reviewer: gpt-6.1-sol
  reasoning_effort: medium
run:
  max_retries: 3           # попыток на задачу
  attempt_timeout_sec: 1800
verify:
  selector_runner: uv run pytest {selectors} -q
  timeout_sec: 900
budget:
  max_tokens: null         # стоп по расходу выключен, пока не задан
  max_usd: null
  prices_per_mtok:         # без цен считаются только токены
    gpt-6.1-sol: { input: 1.25, output: 10.0 }
paths:
  spec_dir: spec
  artifacts_dir: .sdd-codex/runs
  context_files: [spec/requirements.md, spec/design.md]
git:
  commit_template: "{task_id}: {task_name}"
```

## Артефакты прогона

В `paths.artifacts_dir` (по умолчанию `.sdd-codex/runs/<timestamp>/`):

- `summary.md` — итог по задачам и расход токенов/денег;
- `run.jsonl` — машиночитаемый журнал событий;
- `TASK-NNN/report.md` — попытки, вердикты ревью, коммит, расход;
- `TASK-NNN/attempt-NN.log` и `attempt-NN.red.txt` — журнал попытки и
  сохранённый вывод красного прогона.

Секреты (значение `OPENAI_API_KEY`) в артефактах маскируются.

## Разработка

```sh
uv run pytest -q         # тесты
uv run ruff check src tests
```
