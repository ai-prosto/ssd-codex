## Milestone 1: sdd-codex - это инструмент автоматизации разработки: он читает задачи из структурированных markdown-спецификаций и выполняет их, запуская кодинг-агентов  (на базе Codex SDK) как CLI-подпроцессы. По сути — «фабрика задач»: человек пишет спеку, runner прогоняет задачи через агента с ретраями, код-ревью, git-автоматизацией и верификацией результата.  CI/CD-подобный оркестратор для LLM-агентов, где центральная идея — не «запустить агента», а сделать его работу проверяемой и неподделываемой: доказанный красный тест, замороженные файлы, аудируемые бюджеты и ревью-гейты.

Сгенерировано task_bridge из behaviour-spec бандла sdd-codex-markdown-codex-sdk-cli-runner-20261117-5076 (шаг 3 плана развития конвейера; группировка задач — по Feature-секциям). Личный путь: бриф одобрен владельцем в разговоре, задачи исполняются сразу.

### TASK-001: Формат spec-runner и единственный конфиг
P2 | 🔄 IN_PROGRESS   Est: 0.5d

Реализовать сценарии BEH-01, BEH-02, BEH-03, BEH-04 (DT-01, группа solo).
Source: workstreams/sdd-codex-markdown-codex-sdk-cli-runner-20261117-5076/spec/30-decomposition.md#DT-01

**Checklist:**
- [ ] реализовать BEH-01: Готовый набор spec-runner разбирается своим парсером
- [ ] реализовать BEH-02: Ошибки формата и зависимостей видны до платных вызовов
- [ ] реализовать BEH-03: Модели по роли в одном конфиге, вендора в схеме нет
- [ ] реализовать BEH-04: Лимиты задаются в конфиге, стоп по расходу выключен по умолчанию
- [ ] проверка группы: tests/test_format.py::test_parses_ready_spec_runner_set (kind: pytest), tests/test_format.py::test_reports_format_and_dependency_errors_without_api_calls (kind: pytest), tests/test_config.py::test_models_per_role_and_no_vendor_key (kind: pytest), tests/test_config.py::test_limits_from_config_budget_disabled_by_default (kind: pytest) зелёные на BEH-01, BEH-02, BEH-03, BEH-04



### TASK-002: CLI и команда создания спеки планировщиком
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-05, BEH-06, BEH-07 (DT-02, группа solo).
Source: workstreams/sdd-codex-markdown-codex-sdk-cli-runner-20261117-5076/spec/30-decomposition.md#DT-02
**Depends on:** [TASK-001]

**Checklist:**
- [ ] реализовать BEH-05: Описание программы — аргументом или из файла
- [ ] реализовать BEH-06: Команда создания спеки пишет ровно три файла в формате spec-runner
- [ ] реализовать BEH-07: Создание спеки и запуск — две отдельные подкоманды, спека не исполняет
- [ ] проверка группы: tests/test_cli_spec.py::test_description_from_argument_or_from_file (kind: pytest), tests/test_cli_spec.py::test_writes_exactly_three_files_in_spec_runner_format (kind: pytest), tests/test_cli_spec.py::test_spec_command_does_not_execute_tasks (kind: pytest) зелёные на BEH-05, BEH-06, BEH-07



### TASK-003: Цикл задачи: тест, доказанный красный прогон, заморозка, коммит
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-08, BEH-09, BEH-10, BEH-11, BEH-12, BEH-13, BEH-14, BEH-15 (DT-03, группа solo).
Source: workstreams/sdd-codex-markdown-codex-sdk-cli-runner-20261117-5076/spec/30-decomposition.md#DT-03
**Depends on:** [TASK-001]

**Checklist:**
- [ ] реализовать BEH-08: Команда проверки берётся из записи задачи
- [ ] реализовать BEH-09: Красный прогон делает сам runner и сохраняет вывод падения
- [ ] реализовать BEH-10: Тест, прошедший на красном шаге, закрывает прогон как провальный
- [ ] реализовать BEH-11: Задача принята только когда та же команда проходит после правки кода
- [ ] реализовать BEH-12: Менять можно только файлы, перечисленные в задаче
- [ ] реализовать BEH-13: Любой лишний изменённый файл — прогон не принят
- [ ] реализовать BEH-14: Коммит только в текущую ветку, без ветвления и PR
- [ ] реализовать BEH-15: Отметка о выполнении ставится в самом `tasks.md`
- [ ] проверка группы: tests/test_verify.py::test_command_comes_from_task_record (kind: pytest), tests/test_task_loop.py::test_runner_proves_red_before_coding (kind: pytest), tests/test_task_loop.py::test_green_at_red_step_fails_attempt (kind: pytest), tests/test_task_loop.py::test_accepted_only_when_same_command_passes_green (kind: pytest), tests/test_freeze.py::test_only_task_listed_files_allowed (kind: pytest), tests/test_freeze.py::test_extra_changed_file_rejects_attempt_and_rolls_back (kind: pytest), tests/test_git.py::test_commits_to_current_branch_without_branching_or_pr (kind: pytest), tests/test_status.py::test_marks_task_done_in_tasks_md (kind: pytest) зелёные на BEH-08, BEH-09, BEH-10, BEH-11, BEH-12, BEH-13, BEH-14, BEH-15



### TASK-004: Гейт код-ревью отдельным агентом
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-16, BEH-17 (DT-04, группа solo).
Source: workstreams/sdd-codex-markdown-codex-sdk-cli-runner-20261117-5076/spec/30-decomposition.md#DT-04
**Depends on:** [TASK-001], [TASK-003]

**Checklist:**
- [ ] реализовать BEH-16: Диф перед коммитом смотрит отдельный агент-ревьюер, автоматически
- [ ] реализовать BEH-17: Вердикт `block` перезапускает задачу с замечаниями и считается ретраем
- [ ] проверка группы: tests/test_review.py::test_reviewer_is_separate_automatic_agent (kind: pytest), tests/test_review.py::test_block_restarts_task_with_comments_as_retry (kind: pytest) зелёные на BEH-16, BEH-17



### TASK-005: Оркестрация прогона: остановка, бюджет, прогресс, артефакты
P2 | TODO   Est: 0.5d

Реализовать сценарии BEH-18, BEH-19, BEH-20, BEH-21, BEH-22, BEH-23, BEH-24, BEH-25 (DT-05, группа solo).
Source: workstreams/sdd-codex-markdown-codex-sdk-cli-runner-20261117-5076/spec/30-decomposition.md#DT-05
**Depends on:** [TASK-001], [TASK-003], [TASK-004]

**Checklist:**
- [ ] реализовать BEH-18: Запуск исполняет готовый набор без шага создания спеки
- [ ] реализовать BEH-19: После N неудачных прогонов задачи прогон останавливается целиком
- [ ] реализовать BEH-20: После остановки следующие задачи не запускаются
- [ ] реализовать BEH-21: Стоп по токенам/деньгам срабатывает только когда включён в конфиге
- [ ] реализовать BEH-22: На запуск задачи печатается строка с id, описанием и временем
- [ ] реализовать BEH-23: На каждый прогон — своя строка с номером, шагом и результатом теста
- [ ] реализовать BEH-24: У каждой задачи своя поддиректория с логом на каждый прогон
- [ ] реализовать BEH-25: В поддиректории задачи лежит итоговый отчёт
- [ ] проверка группы: tests/test_run_command.py::test_runs_ready_set_without_planning_step (kind: pytest), tests/test_runner.py::test_stops_whole_run_after_max_retries (kind: pytest), tests/test_runner.py::test_following_tasks_not_started_after_stop (kind: pytest), tests/test_budget.py::test_budget_stop_only_when_configured (kind: pytest), tests/test_progress.py::test_prints_task_start_line (kind: pytest), tests/test_progress.py::test_prints_line_per_attempt_with_step_and_result (kind: pytest), tests/test_artifacts.py::test_per_task_dir_with_log_per_attempt (kind: pytest), tests/test_artifacts.py::test_task_report_written_on_success_and_failure (kind: pytest) зелёные на BEH-18, BEH-19, BEH-20, BEH-21, BEH-22, BEH-23, BEH-24, BEH-25



