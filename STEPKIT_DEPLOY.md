# StepKit: публикация курсов на Stepik

[stepik_deploy](https://github.com/tatyderb/stepik_deploy) — отдельный проект
для загрузки и обновления материалов курса на Stepik через API. Он преобразует
уроки из Markdown и формата GIFT в шаги Stepik и публикует их командой
`python deploy.py <файл-урока.md>`.

Это дополнение, а не замена `main.py` в данном репозитории:

- `main.py` — скачивает резервную копию существующего курса;
- StepKit — загружает подготовленные уроки и шаги на Stepik.

Для знакомства со StepKit и форматами шагов доступен бесплатный курс:
[«Работа со StepKit»](https://stepik.org/course/253149).
