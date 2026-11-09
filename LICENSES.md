# Лицензионный аудит конвертера

## Окно pywebview, 2026-11-06

Пользователь явно разрешил WebView2 Runtime и системный .NET для pywebview, а также typing_extensions под PSF-2.0. Исключения ограничены этой задачей/компонентами; другие ограничения сохраняются. Лицензии проверены по METADATA и LICENSE скачанных точных артефактов в `local_web/qa/desktop-v10/wheels`.

| Компонент | Версия | Первичный источник | SPDX / вердикт | Дата |
|---|---|---|---|---|
| Локальный MCP разбора отчётов, очередь исправлений и сопоставление HTML-предпросмотра | 1.8.0 | local_web/report_intake.py, report_mcp.py, discrepancies.py, web/review.js | NO-LICENSE, разрешено; новых сторонних зависимостей нет | 2026-11-08 |
| Telegram Bot API transport, защищённая настройка бота и миграция очереди | 1.7.0 | local_web/telegram_transport.py, report_telegram_config.py, secure_config.py, reporting.py, configure_report_telegram.py | NO-LICENSE, разрешено; только стандартная библиотека Python и ранее разрешённые зависимости | 2026-11-08 |
| Интерфейс «Инженерная среда», добровольное сравнение, очередь отчётов и настройка SMTP | 1.6.0 | local_web/web/workspace.css, review.js, discrepancies.py, reporting.py, report_mail_config.py, configure_report_mail.py | NO-LICENSE, разрешено по правилу пользователя; новых сторонних компонентов нет | 2026-11-08 |
| pywebview | 6.2.1 | https://pypi.org/project/pywebview/6.2.1/ | BSD-3-Clause, разрешено | 2026-11-06 |
| Собственное окно, запуск EXE и склейка шапки с подзаголовком | 1.5.0 | local_web/desktop.py, launcher, convert_docling.py | NO-LICENSE, разрешено по правилу пользователя | 2026-11-06 |
| Иконка приложения из вложения пользователя, многомерный ICO и подключение к EXE/pywebview | 1.5.1 | local_web/assets/app-icon.webp, app-icon.ico, launcher/build.ps1 | NO-LICENSE, использование прямо запрошено пользователем; новых зависимостей нет | 2026-11-06 |
| Белая иконка истории, прозрачная иконка приложения и упрощённая верхняя панель | 1.5.2 | Изображения пользователя, обработка в local_web/qa/ui-v12/process_icons.py по явному указанию через Pillow | NO-LICENSE, разрешено; Pillow той же установленной версии, новых компонентов нет | 2026-11-06 |
| pythonnet | 3.0.5 | https://pypi.org/project/pythonnet/3.0.5/ | MIT, разрешено | 2026-11-06 |
| clr_loader | 0.2.7.post0 | https://pypi.org/project/clr-loader/0.2.7.post0/ | MIT, разрешено | 2026-11-06 |
| proxy_tools | 0.1.0 | https://pypi.org/project/proxy-tools/0.1.0/ | MIT, разрешено | 2026-11-06 |
| bottle | 0.13.4 | https://pypi.org/project/bottle/0.13.4/ | MIT, разрешено | 2026-11-06 |
| CFFI, отдельный каталог desktop_runtime | 1.17.1 | https://pypi.org/project/cffi/1.17.1/ | MIT, разрешено; установленная CFFI OCR не заменяется | 2026-11-06 |
| typing_extensions | 4.16.0 | https://pypi.org/project/typing-extensions/4.16.0/ | PSF-2.0, разрешено пользователем для pywebview | 2026-11-06 |
| WebView2 Runtime / WebView2 SDK DLL и системный .NET | установленные системные / включённые pywebview | https://developer.microsoft.com/en-us/microsoft-edge/webview2/ | Проприетарные, разрешены пользователем для окна pywebview | 2026-11-06 |

Дата проверки: **2026-11-02**. Правило: белый список и явно согласованные индивидуальные исключения из `AGENTS.md`, а не общая совместимость open source лицензий.

**Лицензионный барьер текущего окружения не пройден.** Этот файл фиксирует исходное состояние; зависимости, движки и веса не устанавливались и не заменялись. Разрешённая лицензия Python-пакета не означает разрешение его весов, словарей, шрифтов или вложенных бинарных компонентов.

## Согласованное исключение OpenRAIL

**По прямому решению пользователя от 2026-11-02 семейство OpenRAIL, включая modified OpenRAIL-M используемых весов Surya 2, разрешено.** Это исключение внесено в `AGENTS.md`; остальные запреты, серая зона и проверки точных артефактов сохраняются.

Surya 2, включая используемые GGUF, имеет modified OpenRAIL-M и **разрешена по этому исключению**. Код `surya-ocr==0.22.1` — Apache-2.0, отдельно от весов. Установленные настройки указывают `datalab-to/surya-ocr-2-gguf`, `surya-2.gguf` и `surya-2-mmproj.gguf`. Локальный snapshot: `6a3a4c30e5e74446d4f8b6afd05b2f2da970f470`. [Карточка GGUF](https://huggingface.co/datalab-to/surya-ocr-2-gguf), [README релиза 0.22.1](https://github.com/datalab-to/surya/blob/v0.22.1/README.md). Исключение для весов не является разрешением всего окружения или неподтверждённых других моделей Surya.

## Согласованные исключения для словаря и tqdm

**По прямому решению пользователя от 2026-11-02 для этой программы постоянно разрешены словарные данные `pymorphy3-dicts-ru` под CC-BY-SA-3.0 и `tqdm` под MPL-2.0 AND MIT.** Это разрешение относится к указанным компонентам и не ограничено данным прогоном или проверенными номерами версий. Фактическая лицензия каждой новой версии по-прежнему проверяется. Другие CC-BY-SA/MPL-компоненты, включая certifi, этим решением не разрешены.

- Проверенные словарные данные `pymorphy3-dicts-ru==2.4.417150.4580142`: **разрешены по индивидуальному исключению CC-BY-SA-3.0**. MIT относится отдельно к коду упаковки. Лицензия данных указана в установленном METADATA и [описании релиза](https://pypi.org/project/pymorphy3-dicts-ru/2.4.417150.4580142/). Источник данных: OpenCorpora, версия 0.92, revision 417150, corpus revision 4580142 (`pymorphy3_dicts_ru/data/meta.json`).
- Проверенный `tqdm==4.70.1`: **разрешён по индивидуальному исключению для совокупности MPL-2.0 AND MIT**. Источник: установленный METADATA, его хеш и путь сохранены в доказательствах первоначального аудита. AND не переинтерпретируется как OR: разрешены обе части именно для tqdm.

## Подтверждённые препятствия

Для проверки Surya-only на всех примерах «Эталоны OCR» пользователь 2026-11-02 отдельно разрешил использовать **уже установленную среду** (включая CUDA/cuDNN, MPL-компоненты, компоненты вне белого списка и текущие веса TableFormer), без установки новых компонентов. Это разрешение среды относится к данному тестовому прогону; постоянные индивидуальные исключения перечислены выше, остальные ограничения проекта сохраняются. Артефакты и точные версии прогона сохраняются в `benchmarks/surya_only_20261102`.

1. **`certifi==2026.10.19` — MPL-2.0, запрещено.** Достижимая транзитивная зависимость HTTP-клиентов; наличие офлайн-кэша не исключает её из поставки. Индивидуальное исключение tqdm не распространяется на certifi. [LICENSE релиза](https://github.com/certifi/python-certifi/blob/2026.10.19/LICENSE).
2. **Поставляемые CUDA/cuDNN библиотеки имеют отдельные NVIDIA EULA, запрещено при буквальном применении белого списка.** В `torch==2.8.0+cu128` физически есть `cudart64_12.dll`, `cublas64_12.dll`, семейство `cudnn*_9.dll` и другие NVIDIA DLL. BSD-3-Clause кода PyTorch их не перелицензирует. [CUDA EULA](https://docs.nvidia.com/cuda/eula/), [разъяснение NVIDIA о самостоятельных SDK-лицензиях](https://www.nvidia.com/en-us/agreements/enterprise-software/nvidia-software-licensing-overview-and-faq/).
3. **В коде есть достижимый внешний OCR `Windows.Media.Ocr`, не прошедший барьер.** `docling_source_text.py:24–28` запускает Windows worker; потребители: `docling_pdf_table_layout.py:295`, `docling_pdf_text_indices.py:61`, `docling_pdf_repairs.py:1057` и `:1391`. Это системный проприетарный движок Windows, а не библиотека под разрешённой лицензией. [Описание API Microsoft](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine), [условия Microsoft](https://www.microsoft.com/en-us/useterms). Рабочий файл `docling_windows_text.ps1` в корне проекта отсутствует: наличие пути в коде не доказывает успешный запуск движка в конкретной конвертации.

## Охват и доказательства

Инвентаризированы **154** установленных Python distribution. Замыкание `requirements-docling-surya.txt` и внешних импортов текущих `convert_docling.py`, `docling_*.py` с учётом Windows markers и активных extras содержит **124** компонента: **123** установлены, `coloredlogs` отсутствует. Это консервативное замыкание включает опциональные ветки; фактическая активация ветки определяется диагностикой запуска. Прочие 31 пакета среды не объявлены автоматически зависимостями конвертера.

- [CSV всех пакетов](benchmarks/ocr_tables_20261102/license_audit/package_inventory.csv): версия, связь, заявленная лицензия, вердикт, источник, дата.
- [Проверяемые доказательства](benchmarks/ocr_tables_20261102/license_audit/dependency_evidence.json): исходные License/License-Expression/Classifiers/Requires-Dist, пути и SHA256 METADATA, импорты, родители зависимостей, локальные LICENSE.
- [Реестр 265 файлов LICENSE/NOTICE](benchmarks/ocr_tables_20261102/license_audit/license_notice_files.csv): пути, размеры и SHA256. Здесь учтены также документы пакетов вне замыкания.
- [Реальные файлы весов и llama-server](benchmarks/ocr_tables_20261102/license_audit/model_artifacts.json): размер и вычисленный SHA256, без запуска или изменения файлов.

CSV/JSON первоначального аудита, включая `summary.json`, сохраняются как исторический снимок до индивидуальных разрешений для словаря и tqdm. Их прежние запрещающие вердикты по этим двум компонентам заменены решениями от 2026-11-02 в настоящем файле; исходные доказательства лицензий остаются применимыми.

**Ограничение аудита:** полный покомпонентный разбор всех статически включённых библиотек конкретных wheels ещё не завершён. Их notices инвентаризированы, но строка «разрешено: только заявленная лицензия пакета» относится к верхнему уровню кода. Она не является итоговым разрешением всего wheel. Список notices для нескольких платформ сам по себе не доказывает включение каждого указанного компонента в установленный Windows binary.

`UNKNOWN` и отсутствие поля License в METADATA **не** означают `NO-LICENSE`. Компоненты с неясной лицензией остаются неподтверждёнными. Ни один такой компонент не признан разрешённым по отсутствию информации.

## Модели, данные, бинарные сборки и шрифты

| Компонент | Версия / revision | Первичный источник | SPDX / идентификатор | Вердикт | Дата |
| --- | --- | --- | --- | --- | --- |
| Docling, код | 2.141.0 | [PyPI](https://pypi.org/project/docling/2.141.0/), установленный METADATA | MIT | Разрешено для кода; зависимости отдельно | 2026-11-02 |
| docling-surya-ocr, код | 0.2.0 | [PyPI](https://pypi.org/project/docling-surya-ocr/0.2.0/) | MIT | Разрешено для кода; Surya 2 веса разрешены по исключению OpenRAIL от 2026-11-02, runtime отдельно | 2026-11-02 |
| Surya, код | 0.22.1 | [Релиз](https://github.com/datalab-to/surya/blob/v0.22.1/LICENSE), установленный LICENSE | Apache-2.0 | Разрешено для кода | 2026-11-02 |
| Surya 2 GGUF и projector | 6a3a4c30e5e74446d4f8b6afd05b2f2da970f470 | [Карточка](https://huggingface.co/datalab-to/surya-ocr-2-gguf) | LicenseRef-Modified-OpenRAIL-M | **Разрешено по прямому решению пользователя от 2026-11-02; исключение OpenRAIL**, runtime отдельно | 2026-11-02 |
| Прочие Surya detector / error / fast layout weights | Настройки 0.22.1; точные файлы этой задачи не установлены | [Surya](https://github.com/datalab-to/surya/blob/v0.22.1/README.md) | UNKNOWN; нельзя переносить лицензию кода на веса | Вопрос пользователю: неподтверждено; не активировать без проверки точных весов | 2026-11-02 |
| Docling layout Heron weights | 8f39ad3c0b4c58e9c2d2c84a38465abf757272d8 | [Карточка](https://huggingface.co/docling-project/docling-layout-heron), локальная README данного snapshot | Apache-2.0 | Разрешено для модели; runtime отдельно | 2026-11-02 |
| Docling TableFormer weights | docling-models v2.3.0, fc0f2d45e2218ea24bce5045f58a389aed16dc23 | [README v2.3.0](https://huggingface.co/docling-project/docling-models/blob/v2.3.0/README.md), локальная README | CDLA-Permissive-2.0 | Вопрос пользователю: вне белого списка. Текущие общие теги Apache/CDLA не доказывают OR для старого snapshot | 2026-11-02 |
| CodeFormulaV2 weights, сохранённые в HF cache | ecedbe111d15c2dc60bfd4a823cbe80127b58af4 | [Карточка](https://huggingface.co/docling-project/CodeFormulaV2), локальная README | CDLA-Permissive-2.0 | Вопрос пользователю; текущий default `p2t` не означает использование этой модели | 2026-11-02 |
| Pix2Text MFR 1.5 ONNX weights / tokenizer | 1cef9f0bdcd6a4c63df7de1311fb0894593340cc | [Карточка](https://huggingface.co/breezedeus/pix2text-mfr-1.5), локальная README, `docling_p2t.py:11–16` | MIT | Разрешена лицензия модели; SHA256 вычислены, независимая проверка ожидаемого upstream hash и runtime остаются | 2026-11-02 |
| Полный пакет Pix2Text и его прочие модели | Не установлены и не импортируются текущим reader | [Pix2Text](https://github.com/breezedeus/Pix2Text) | Не применимо к установленному reader | Не включены; не утверждается аудит всех моделей Pix2Text | 2026-11-02 |
| RapidOCR, код | 3.10.0 | [PyPI](https://pypi.org/project/rapidocr/3.10.0/), установленный LICENSE | Apache-2.0 | Разрешено для кода | 2026-11-02 |
| RapidOCR PP-OCRv6_det_small ONNX | Registry v3.10.0, SHA256 090f04abcd9d9a7498bc4ebf677e4cb9bdce1fe4197ddb7e529f1ef44e1ff94f | [Upstream card](https://huggingface.co/PaddlePaddle/PP-OCRv6_small_det_onnx), [notice RapidOCR](https://rapidai.github.io/RapidOCRDocs/main/en/model_licenses/), установленный registry | Apache-2.0 | Разрешено для весов, локальный hash совпал с registry; runtime отдельно | 2026-11-02 |
| RapidOCR Cyrillic PP-OCRv5 mobile rec ONNX | Registry v3.10.0, SHA256 90f761b4bfcce0c8c561c0cb5c887b0971d3ec01c32164bdf7374a35b0982711 | [Upstream card](https://huggingface.co/PaddlePaddle/cyrillic_PP-OCRv5_mobile_rec), [registry](https://github.com/RapidAI/RapidOCR/blob/main/python/rapidocr/default_models.yaml), локальный registry | Apache-2.0 upstream; идентичность преобразования установлена registry | Вопрос пользователю: release notice перечисляет только bundled default models, отдельный converted Cyrillic artifact требует подтверждения условий дистрибуции RapidAI; локальный hash совпал | 2026-11-02 |
| RapidOCR default PP-OCRv6_rec_small и ch_ppocr_mobile_v2.0_cls | Registry 3.10.0; hashes в установленном MODEL_LICENSES.md | [Model notice](https://rapidai.github.io/RapidOCRDocs/main/en/model_licenses/) | Apache-2.0 | Разрешено для перечисленных notice весов; фактическая загрузка в ориентации зависит от ветки | 2026-11-02 |
| PP-LCNet_x1_0_doc_ori_onnx | 7330ab7039123e46af2dc03154b9969aa412c61d; ожидаемый SHA256 af9a0a4f317ff0709ce752067807f819cb15d883f8ecad89f28df1c6ee2d9c92 | [Карточка PaddlePaddle](https://huggingface.co/PaddlePaddle/PP-LCNet_x1_0_doc_ori_onnx), `convert_docling.py:55–56` | Apache-2.0 | Разрешена лицензия модели; конвертер проверяет hash файла | 2026-11-02 |
| pymorphy3-dicts-ru, код | 2.4.417150.4580142 | [Описание релиза](https://pypi.org/project/pymorphy3-dicts-ru/2.4.417150.4580142/) | MIT | Разрешено для кода упаковки; данные отдельно разрешены по индивидуальному исключению | 2026-11-02 |
| pymorphy3-dicts-ru, словарные данные | Пакет 2.4.417150.4580142; OpenCorpora 0.92 / revision 417150 / corpus 4580142 | Тот же [релиз](https://pypi.org/project/pymorphy3-dicts-ru/2.4.417150.4580142/), установленный data/meta.json | CC-BY-SA-3.0 | **Разрешено по постоянному индивидуальному решению пользователя от 2026-11-02 для pymorphy3-dicts-ru** | 2026-11-02 |
| llama.cpp, код | Точный build установленного launcher не установлен | [LICENSE](https://github.com/ggml-org/llama.cpp/blob/master/LICENSE) | MIT | Разрешён upstream код; это не аудит скачанной сборки | 2026-11-02 |
| Установленный llama-server.exe и соседние DLL | `%LOCALAPPDATA%/docling-surya/llamacpp-cuda`; hash launcher в model_artifacts.json | [Релизы upstream](https://github.com/ggml-org/llama.cpp/releases), источник точного установленного build не подтверждён | UNKNOWN + NVIDIA EULA для CUDA-компонентов | Вопрос пользователю / неподтверждено; CUDA часть запрещена | 2026-11-02 |
| Windows.Media.Ocr и языковые OCR-модели | Системная Windows; точные версии моделей не установлены | [Microsoft API](https://learn.microsoft.com/en-us/uwp/api/windows.media.ocr.ocrengine), [условия](https://www.microsoft.com/en-us/useterms) | LicenseRef-Microsoft-Windows | **Запрещено как проприетарный OCR**; worker отсутствует в текущем корне | 2026-11-02 |
| GoNoto fonts, указанные настройками Surya | Конфигурация ссылается на release v7.0; локальные TTF в distribution отсутствуют | [Upstream fonts](https://github.com/satbyy/go-noto-universal), настройки surya-ocr | UNKNOWN для фактического скачанного артефакта | Вопрос пользователю; не использовались и не скачивались при аудите | 2026-11-02 |
| PaddleOCR-VL-1.6, кандидат, не установлен | Точный snapshot ещё не выбран | [Карточка модели](https://huggingface.co/PaddlePaddle/PaddleOCR-VL-1.6), [код](https://github.com/PaddlePaddle/PaddleOCR) | Apache-2.0 заявлена для кода и весов | Лицензии верхнего уровня допустимы; **полный барьер runtime и транзитивов не пройден**, не подключён | 2026-11-02 |

## Вложенные компоненты и серая зона

| Компонент | Версия | Источник | SPDX / идентификатор | Вердикт | Дата |
| --- | --- | --- | --- | --- | --- |
| Pillow, основной код | 10.4.0 | [LICENSE нужного релиза](https://github.com/python-pillow/Pillow/blob/10.4.0/LICENSE) | HPND | Вопрос пользователю: вне белого списка | 2026-11-02 |
| NumPy, заявленная совокупная лицензия | 2.5.4 | Установленный METADATA / LICENSE | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | Вопрос пользователю: 0BSD, Zlib и CC0 не разрешены автоматически | 2026-11-02 |
| Clipper внутри pyclipper | Wrapper 1.4.0, Clipper 6.4.2 | [README upstream](https://github.com/fonttools/pyclipper) | MIT для wrapper AND BSL-1.0 для Clipper | Вопрос пользователю: BSL-1.0 | 2026-11-02 |
| PDFium wrapper / native binary | Установлено pypdfium2 5.10.1; requirements требует 5.14.0 | [Проект](https://github.com/pypdfium2-team/pypdfium2), локальные BUILD_LICENSES и version.json | BSD-3-Clause AND Apache-2.0 + самостоятельные лицензии native dependencies | Вопрос пользователю: транзитивный native аудит не завершён; версии среды и требований расходятся | 2026-11-02 |
| FreeType в Pillow | 2.13.2 | Установленный Pillow LICENSE, секция freetype-2.13.2 | FTL OR GPL-2.0-or-later; выбирается GPL-3.0-or-later | Разрешена выбранная альтернатива ядра FreeType; отдельно встроенный gzip/Zlib требует решения | 2026-11-02 |
| zlib в Pillow | 1.3.1 | Установленный Pillow LICENSE | Zlib | Вопрос пользователю | 2026-11-02 |
| libpng в Pillow | 1.6.43 | Установленный Pillow LICENSE, секция lpng1643 | Libpng | Вопрос пользователю: вне белого списка | 2026-11-02 |
| libjpeg-turbo в Pillow | 3.0.3 | Установленный Pillow LICENSE | Совокупность BSD-3-Clause / IJG / Zlib; точная применимость частей требует разбора | Вопрос пользователю: IJG/Zlib и полный состав | 2026-11-02 |
| Brotli / HarfBuzz / lcms2 / libwebp / OpenJPEG / libtiff / xz в Pillow | 1.1.0 / 8.5.0 / 2.16 / 1.4.0 / 2.5.2 / 4.6.0 / 5.4.5 | Именованные секции установленного Pillow LICENSE; файл хеширован | Различные; полный SPDX каждого вложенного файла не установлен | Вопрос пользователю: неподтверждено, не считается разрешённым по лицензии Pillow | 2026-11-02 |
| OpenCV Windows wheel и third-party payload | 4.11.0.86 | Установленные cv2/LICENSE.txt и LICENSE-3RD-PARTY.txt | Apache-2.0 основного кода; прочие лицензии в notices | Вопрос пользователю: состав конкретной сборки, включая codec/runtime payload, требует отдельной проверки | 2026-11-02 |
| ONNX Runtime GPU и third-party payload | 1.23.2 | [LICENSE релиза](https://github.com/microsoft/onnxruntime/blob/v1.23.2/LICENSE), установленный ThirdPartyNotices.txt | MIT основного кода; в notices присутствуют MPL, Zlib и др. | Вопрос пользователю: определить применимость к данной сборке; не объявлять весь wheel MIT | 2026-11-02 |
| PyTorch / Torchvision, основной код | 2.8.0+cu128 / 0.23.0+cu128 | Установленные LICENSE / NOTICE | BSD-3-Clause | Разрешено для основного кода; CUDA payload запрещён, остальные native части требуют аудита | 2026-11-02 |
| NVIDIA CUDA / cuDNN payload | CUDA build cu128; точные версии каждой DLL отдельно не установлены | [EULA](https://docs.nvidia.com/cuda/eula/), фактические DLL torch/lib | LicenseRef-NVIDIA-CUDA-EULA / LicenseRef-NVIDIA-cuDNN-EULA | **Запрещено** | 2026-11-02 |
| Python-derived licenses: typing_extensions, defusedxml и пр. | Версии в таблице пакетов | Установленные METADATA / LICENSE | PSF-2.0 / PSFL / CNRI-Python | Вопрос пользователю: вне белого списка; наличие в стандартной экосистеме не даёт автоматического допуска | 2026-11-02 |

Наличие MPL/GPL/другой лицензии в агрегированном third-party notice само по себе не доказывает включение соответствующего кода в конкретную сборку. Именно поэтому неподтверждённые native payload отмечены вопросом, а подтверждённые самостоятельные запрещённые компоненты перечислены отдельно.

## Кандидат на последующий замер

Готовой замены **со всем окружением, прошедшим барьер**, в этом аудите не найдено. RapidOCR и PaddleOCR-VL-1.6 имеют допустимые лицензии верхнего уровня, но подключать их как якобы полностью проверенную замену Surya сейчас нельзя. Сначала нужны: закрытая спецификация CPU-окружения или согласованное исключение для CUDA, проверка точных wheels и всех их notices, решение по HPND/Zlib/CC0/BSL и другим серым лицензиям, устранение оставшихся запрещённых HTTP-транзитивов из новой поставки, pinned revisions и проверяемые SHA256 весов. Данные pymorphy3-dicts-ru и tqdm отдельно разрешены постоянными индивидуальными исключениями выше. После этого — одинаковые страницы эталонов, CER/WER, числа/единицы, сетка/rowspan/colspan, формулы и ресурсы. Сравнительного заявления «лучше» сейчас нет.

Воспроизведение инвентаризации без OCR, скачивания моделей и установки пакетов:

```powershell
python benchmarks/ocr_tables_20261102/license_audit/collect_inventory.py
```

## Реестр Python-пакетов замыкания проекта

В колонке «Источник» указан точный релиз PyPI; фактически проверенные установленные METADATA/LICENSE, их абсолютные пути и хеши находятся в `dependency_evidence.json`. Строка «разрешено» ниже всегда ограничена указанной заявленной лицензией пакета; оговорки о весах, данных и native payload выше имеют приоритет.

| Компонент | Версия | Связь | SPDX кода / пакета | Вердикт | Источник | Дата |
| --- | --- | --- | --- | --- | --- | --- |
| accelerate | 1.15.0 | transitive | Apache-2.0 | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/accelerate/1.15.0/) | 2026-11-02 |
| annotated-doc | 0.0.5 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/annotated-doc/0.0.5/) | 2026-11-02 |
| annotated-types | 0.8.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/annotated-types/0.8.0/) | 2026-11-02 |
| anyio | 4.15.1 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/anyio/4.15.1/) | 2026-11-02 |
| attrs | 26.1.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/attrs/26.1.0/) | 2026-11-02 |
| beautifulsoup4 | 4.15.0 | direct_or_import | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/beautifulsoup4/4.15.0/) | 2026-11-02 |
| certifi | 2026.10.19 | transitive | MPL-2.0 | запрещено по AGENTS.md | [METADATA/PyPI](https://pypi.org/project/certifi/2026.10.19/) | 2026-11-02 |
| cffi | 2.1.1 | transitive | MIT-0 | вопрос пользователю: вне белого списка / неподтверждено | [METADATA/PyPI](https://pypi.org/project/cffi/2.1.1/) | 2026-11-02 |
| charset-normalizer | 3.5.2 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/charset-normalizer/3.5.2/) | 2026-11-02 |
| click | 8.5.0 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/click/8.5.0/) | 2026-11-02 |
| colorama | 0.4.6 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/colorama/0.4.6/) | 2026-11-02 |
| colorlog | 6.12.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/colorlog/6.12.0/) | 2026-11-02 |
| cryptography | 50.0.2 | transitive | Apache-2.0 OR BSD-3-Clause | разрешено: выбрана Apache-2.0, только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/cryptography/50.0.2/) | 2026-11-02 |
| DAWG2-Python | 0.9.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/DAWG2-Python/0.9.0/) | 2026-11-02 |
| defusedxml | 0.7.1 | transitive | PSFL | вопрос пользователю: вне белого списка / неподтверждено | [METADATA/PyPI](https://pypi.org/project/defusedxml/0.7.1/) | 2026-11-02 |
| dill | 0.4.1 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/dill/0.4.1/) | 2026-11-02 |
| distro | 1.9.0 | transitive | Apache-2.0 | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/distro/1.9.0/) | 2026-11-02 |
| doclang | 0.7.3 | transitive | Apache-2.0 | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/doclang/0.7.3/) | 2026-11-02 |
| docling | 2.141.0 | direct_or_import | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/docling/2.141.0/) | 2026-11-02 |
| docling-core | 2.103.1 | direct_or_import | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/docling-core/2.103.1/) | 2026-11-02 |
| docling-ibm-models | 4.0.4 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/docling-ibm-models/4.0.4/) | 2026-11-02 |
| docling-parse | 7.22.4 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/docling-parse/7.22.4/) | 2026-11-02 |
| docling-slim | 2.141.0 | direct_or_import | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/docling-slim/2.141.0/) | 2026-11-02 |
| docling-surya-ocr | 0.2.0 | direct_or_import | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/docling-surya-ocr/0.2.0/) | 2026-11-02 |
| et_xmlfile | 2.0.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/et_xmlfile/2.0.0/) | 2026-11-02 |
| Faker | 40.43.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/Faker/40.43.0/) | 2026-11-02 |
| filelock | 3.32.7 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/filelock/3.32.7/) | 2026-11-02 |
| filetype | 1.2.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/filetype/1.2.0/) | 2026-11-02 |
| flatbuffers | 25.12.19 | transitive | Apache-2.0 | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/flatbuffers/25.12.19/) | 2026-11-02 |
| fsspec | 2026.9.0 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/fsspec/2026.9.0/) | 2026-11-02 |
| h11 | 0.16.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/h11/0.16.0/) | 2026-11-02 |
| hf-xet | 1.7.0 | transitive | Apache-2.0 | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/hf-xet/1.7.0/) | 2026-11-02 |
| httpcore | 1.0.9 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/httpcore/1.0.9/) | 2026-11-02 |
| httpx | 0.28.1 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/httpx/0.28.1/) | 2026-11-02 |
| huggingface_hub | 1.33.0 | direct_or_import | Apache-2.0 | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/huggingface_hub/1.33.0/) | 2026-11-02 |
| idna | 3.20 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/idna/3.20/) | 2026-11-02 |
| Jinja2 | 3.1.6 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/Jinja2/3.1.6/) | 2026-11-02 |
| jiter | 0.17.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/jiter/0.17.0/) | 2026-11-02 |
| jsonref | 1.1.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/jsonref/1.1.0/) | 2026-11-02 |
| jsonschema | 4.26.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/jsonschema/4.26.0/) | 2026-11-02 |
| jsonschema-specifications | 2025.9.1 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/jsonschema-specifications/2025.9.1/) | 2026-11-02 |
| langcodes | 3.5.1 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/langcodes/3.5.1/) | 2026-11-02 |
| latex2mathml | 3.81.1 | direct_or_import | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/latex2mathml/3.81.1/) | 2026-11-02 |
| lxml | 6.1.3 | direct_or_import | BSD-3-Clause | вопрос пользователю: код и бинарные транзитивы проверяются отдельно | [METADATA/PyPI](https://pypi.org/project/lxml/6.1.3/) | 2026-11-02 |
| mail-parser | 4.8.0 | transitive | Apache-2.0 | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/mail-parser/4.8.0/) | 2026-11-02 |
| markdown-it-py | 4.2.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/markdown-it-py/4.2.0/) | 2026-11-02 |
| marko | 2.2.4 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/marko/2.2.4/) | 2026-11-02 |
| MarkupSafe | 3.0.4 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/MarkupSafe/3.0.4/) | 2026-11-02 |
| mdurl | 0.1.2 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/mdurl/0.1.2/) | 2026-11-02 |
| mpire | 2.10.2 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/mpire/2.10.2/) | 2026-11-02 |
| mpmath | 1.3.0 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/mpmath/1.3.0/) | 2026-11-02 |
| multiprocess | 0.70.19 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/multiprocess/0.70.19/) | 2026-11-02 |
| networkx | 3.7 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/networkx/3.7/) | 2026-11-02 |
| numpy | 2.5.4 | direct_or_import | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 | вопрос пользователю: вне белого списка / неподтверждено | [METADATA/PyPI](https://pypi.org/project/numpy/2.5.4/) | 2026-11-02 |
| olefile | 0.47 | transitive | BSD-2-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/olefile/0.47/) | 2026-11-02 |
| omegaconf | 2.4.0 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/omegaconf/2.4.0/) | 2026-11-02 |
| onnxruntime-gpu | 1.23.2 | direct_or_import | MIT | вопрос пользователю: код и бинарные транзитивы проверяются отдельно | [METADATA/PyPI](https://pypi.org/project/onnxruntime-gpu/1.23.2/) | 2026-11-02 |
| openai | 2.54.0 | transitive | Apache-2.0 | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/openai/2.54.0/) | 2026-11-02 |
| opencv-python-headless | 4.11.0.86 | direct_or_import | Apache-2.0 | вопрос пользователю: код и бинарные транзитивы проверяются отдельно | [METADATA/PyPI](https://pypi.org/project/opencv-python-headless/4.11.0.86/) | 2026-11-02 |
| openpyxl | 3.1.5 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/openpyxl/3.1.5/) | 2026-11-02 |
| packaging | 26.3 | transitive | Apache-2.0 OR BSD-2-Clause | разрешено: выбрана Apache-2.0, только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/packaging/26.3/) | 2026-11-02 |
| pandas | 3.0.6 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/pandas/3.0.6/) | 2026-11-02 |
| pdfminer.six | 20231228 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/pdfminer.six/20231228/) | 2026-11-02 |
| pdfplumber | 0.11.4 | direct_or_import | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/pdfplumber/0.11.4/) | 2026-11-02 |
| pillow | 10.4.0 | direct_or_import | HPND | вопрос пользователю: вне белого списка / неподтверждено | [METADATA/PyPI](https://pypi.org/project/pillow/10.4.0/) | 2026-11-02 |
| platformdirs | 4.13.5 | direct_or_import | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/platformdirs/4.13.5/) | 2026-11-02 |
| pluggy | 1.7.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/pluggy/1.7.0/) | 2026-11-02 |
| polyfactory | 3.5.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/polyfactory/3.5.0/) | 2026-11-02 |
| protobuf | 7.36.2 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/protobuf/7.36.2/) | 2026-11-02 |
| psutil | 7.2.2 | direct_or_import | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/psutil/7.2.2/) | 2026-11-02 |
| pyclipper | 1.4.0 | transitive | MIT | вопрос пользователю: wrapper MIT, Clipper BSL-1.0 | [METADATA/PyPI](https://pypi.org/project/pyclipper/1.4.0/) | 2026-11-02 |
| pycparser | 3.12 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/pycparser/3.12/) | 2026-11-02 |
| pydantic | 2.14.1 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/pydantic/2.14.1/) | 2026-11-02 |
| pydantic_core | 2.50.1 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/pydantic_core/2.50.1/) | 2026-11-02 |
| pydantic-settings | 2.16.1 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/pydantic-settings/2.16.1/) | 2026-11-02 |
| Pygments | 2.21.0 | transitive | BSD-2-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/Pygments/2.21.0/) | 2026-11-02 |
| pylatexenc | 2.11 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/pylatexenc/2.11/) | 2026-11-02 |
| pymorphy3 | 2.0.6 | direct_or_import | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/pymorphy3/2.0.6/) | 2026-11-02 |
| pymorphy3-dicts-ru | 2.4.417150.4580142 | direct_or_import | MIT для упаковки; CC-BY-SA-3.0 для данных | разрешено: код MIT; данные по постоянному индивидуальному решению пользователя от 2026-11-02 | [METADATA/PyPI](https://pypi.org/project/pymorphy3-dicts-ru/2.4.417150.4580142/) | 2026-11-02 |
| pypdfium2 | 5.10.1 | direct_or_import | BSD-3-Clause AND Apache-2.0 | вопрос пользователю: код и бинарные транзитивы проверяются отдельно | [METADATA/PyPI](https://pypi.org/project/pypdfium2/5.10.1/) | 2026-11-02 |
| python-dateutil | 3.0.0 | transitive | Apache-2.0 AND BSD-3-Clause | разрешено: обе лицензии разрешены, только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/python-dateutil/3.0.0/) | 2026-11-02 |
| python-docx | 1.2.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/python-docx/1.2.0/) | 2026-11-02 |
| python-dotenv | 1.2.4 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/python-dotenv/1.2.4/) | 2026-11-02 |
| python-oxmsg | 0.0.2 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/python-oxmsg/0.0.2/) | 2026-11-02 |
| python-pptx | 1.0.2 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/python-pptx/1.0.2/) | 2026-11-02 |
| pywin32 | 312 | transitive | PSF | вопрос пользователю: вне белого списка / неподтверждено | [METADATA/PyPI](https://pypi.org/project/pywin32/312/) | 2026-11-02 |
| PyYAML | 6.0.3 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/PyYAML/6.0.3/) | 2026-11-02 |
| rapidocr | 3.10.0 | direct_or_import | Apache-2.0 | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/rapidocr/3.10.0/) | 2026-11-02 |
| referencing | 0.37.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/referencing/0.37.0/) | 2026-11-02 |
| regex | 2026.9.29 | transitive | Apache-2.0 AND CNRI-Python | вопрос пользователю: вне белого списка / неподтверждено | [METADATA/PyPI](https://pypi.org/project/regex/2026.9.29/) | 2026-11-02 |
| requests | 2.34.2 | transitive | Apache-2.0 | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/requests/2.34.2/) | 2026-11-02 |
| rich | 15.0.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/rich/15.0.0/) | 2026-11-02 |
| rpds-py | 2026.9.1 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/rpds-py/2026.9.1/) | 2026-11-02 |
| rtree | 1.4.1 | transitive | MIT | вопрос пользователю: код и бинарные транзитивы проверяются отдельно | [METADATA/PyPI](https://pypi.org/project/rtree/1.4.1/) | 2026-11-02 |
| safetensors | 0.8.0 | transitive | Apache-2.0 | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/safetensors/0.8.0/) | 2026-11-02 |
| scipy | 1.18.1 | transitive | BSD-3-Clause | вопрос пользователю: код и бинарные транзитивы проверяются отдельно | [METADATA/PyPI](https://pypi.org/project/scipy/1.18.1/) | 2026-11-02 |
| semchunk | 3.2.5 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/semchunk/3.2.5/) | 2026-11-02 |
| setuptools | 84.2.1 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/setuptools/84.2.1/) | 2026-11-02 |
| shapely | 2.2.0 | transitive | BSD-3-Clause | вопрос пользователю: код и бинарные транзитивы проверяются отдельно | [METADATA/PyPI](https://pypi.org/project/shapely/2.2.0/) | 2026-11-02 |
| shellingham | 1.5.4 | transitive | ISC | вопрос пользователю: вне белого списка / неподтверждено | [METADATA/PyPI](https://pypi.org/project/shellingham/1.5.4/) | 2026-11-02 |
| six | 1.17.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/six/1.17.0/) | 2026-11-02 |
| sniffio | 1.3.1 | transitive | MIT OR Apache-2.0 | разрешено: выбрана MIT, только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/sniffio/1.3.1/) | 2026-11-02 |
| soupsieve | 3.0.3 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/soupsieve/3.0.3/) | 2026-11-02 |
| surya-ocr | 0.22.1 | direct_or_import | Apache-2.0 | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/surya-ocr/0.22.1/) | 2026-11-02 |
| sympy | 1.14.0 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/sympy/1.14.0/) | 2026-11-02 |
| tabulate | 0.10.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/tabulate/0.10.0/) | 2026-11-02 |
| tokenizers | 0.23.3 | direct_or_import | Apache-2.0 | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/tokenizers/0.23.3/) | 2026-11-02 |
| torch | 2.8.0+cu128 | direct_or_import | BSD-3-Clause | вопрос пользователю: код и бинарные транзитивы проверяются отдельно | [METADATA/PyPI](https://pypi.org/project/torch/2.8.0/) | 2026-11-02 |
| torchvision | 0.23.0+cu128 | direct_or_import | BSD-3-Clause | вопрос пользователю: код и бинарные транзитивы проверяются отдельно | [METADATA/PyPI](https://pypi.org/project/torchvision/0.23.0/) | 2026-11-02 |
| tqdm | 4.70.1 | transitive | MPL-2.0 AND MIT | разрешено по постоянному индивидуальному решению пользователя от 2026-11-02 для tqdm | [METADATA/PyPI](https://pypi.org/project/tqdm/4.70.1/) | 2026-11-02 |
| transformers | 5.20.2 | direct_or_import | Apache-2.0 | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/transformers/5.20.2/) | 2026-11-02 |
| tree-sitter | 0.26.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/tree-sitter/0.26.0/) | 2026-11-02 |
| tree-sitter-c | 0.24.2 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/tree-sitter-c/0.24.2/) | 2026-11-02 |
| tree-sitter-javascript | 0.25.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/tree-sitter-javascript/0.25.0/) | 2026-11-02 |
| tree-sitter-python | 0.25.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/tree-sitter-python/0.25.0/) | 2026-11-02 |
| tree-sitter-typescript | 0.23.2 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/tree-sitter-typescript/0.23.2/) | 2026-11-02 |
| typer | 0.26.8 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/typer/0.26.8/) | 2026-11-02 |
| typing_extensions | 4.16.0 | transitive | PSF-2.0 | вопрос пользователю: вне белого списка / неподтверждено | [METADATA/PyPI](https://pypi.org/project/typing_extensions/4.16.0/) | 2026-11-02 |
| typing-inspection | 0.4.4 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/typing-inspection/0.4.4/) | 2026-11-02 |
| tzdata | 2026.5 | transitive | Apache-2.0 | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/tzdata/2026.5/) | 2026-11-02 |
| urllib3 | 2.8.0 | transitive | MIT | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/urllib3/2.8.0/) | 2026-11-02 |
| websockets | 16.1.1 | transitive | BSD-3-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/websockets/16.1.1/) | 2026-11-02 |
| xlsxwriter | 3.3.0 | transitive | BSD-2-Clause | разрешено: только заявленная лицензия пакета | [METADATA/PyPI](https://pypi.org/project/xlsxwriter/3.3.0/) | 2026-11-02 |

## Локальная веб-оболочка — 2026-11-05

Веб-оболочка не добавляет сторонних пакетов, моделей, шрифтов, CDN или скачиваемых бинарников. HTTP-сервер и очередь используют стандартную библиотеку уже установленного Python; HTML разбирается уже учтённым BeautifulSoup. Существующий OCR-конвертер вызывается отдельным процессом, его версии и лицензионные препятствия выше не менялись.

| Компонент | Версия | Источник | SPDX / идентификатор | Вердикт | Дата |
| --- | --- | --- | --- | --- | --- |
| Собственный код локальной веб-оболочки, интерфейс и XLSX OOXML-экспорт | 1.0.0 | `local_web/` — исходники в проекте | NO-LICENSE | Разрешено согласно правилу пользователя для кода без лицензии; сторонние исходники не копировались | 2026-11-05 |
| Собственный экспорт таблиц/LaTeX/JSON, внешние кропы, ZIP-папки и поддержка длинных путей | 1.1.0, схема экспорта 2 | `local_web/structured_export.py`, `assets.py`, `export_bundle.py`, `file_paths.py`; финализация в `convert_docling.py` | NO-LICENSE | Разрешено по правилу пользователя; сторонние исходники не копировались. Используются уже учтённые Python, BeautifulSoup и Pillow тех же версий; новых библиотек/весов/кодеков не установлено | 2026-11-05 |
| Собственное локальное сохранение, окно «Установленное», открытие папок и полные журналы | 1.2.0 | `local_web/saved_results.py`, `conversion_logs.py`, HTTP API и интерфейс приложения | NO-LICENSE | Разрешено по правилу пользователя; только стандартная библиотека и системное открытие папки. Новых сторонних зависимостей и моделей нет | 2026-11-05 |
| Собственные правила форм, публикация без технических вставок, HTML в каждой папке и открытие файлов | 1.3.0 | Модули конвертера и `local_web/` | NO-LICENSE | Разрешено по правилу пользователя; новых сторонних библиотек, моделей и бинарников нет | 2026-11-05 |
| Собственная склейка подписанных продолжений таблиц после сборки HTML | 1.3.1, схема экспорта 4 | `local_web/table_continuations.py`, экспорт и публикация HTML | NO-LICENSE | Разрешено по правилу пользователя; стандартная библиотека и уже учтённый BeautifulSoup, без новых зависимостей и моделей | 2026-11-05 |
| Собственная проверка дублей при отсутствующем табличном HTML и выбор причины ошибки | 1.3.2 | `docling_pdf_table_rasters.py`, `local_web/converter.py` | NO-LICENSE | Разрешено по правилу пользователя; только существующие зависимости, новые компоненты и модели не добавлены | 2026-11-05 |
| Собственное восстановление полной сетки сканов, частично распознанных текстовых ячеек и печатных номеров | 1.4.0 | `docling_pdf_scan_recovery.py`, `docling_pdf_primary_cells.py`, `convert_docling.py`, экспорт и повторная обработка | NO-LICENSE | Разрешено по правилу пользователя; используются уже учтённые Surya, Pillow, OpenCV, NumPy и BeautifulSoup тех же версий; новых пакетов, весов и шрифтов нет | 2026-11-05 |
| Собственная подстановка звёздочных сносок в ячейки таблиц | 1.4.1 | `local_web/table_star_notes.py`, публикация HTML и экспорт | NO-LICENSE | Разрешено по правилу пользователя; только стандартная библиотека и уже учтённый BeautifulSoup той же версии; новых зависимостей, моделей и шрифтов нет | 2026-11-06 |
| Собственное сопоставление текста движка с проверенной сеткой PDF и поддержка угловых звёздочных сносок | 1.4.2 | `docling_pdf_primary*.py`, `convert_docling.py`, `local_web/table_star_notes.py`, `structured_export.py` | NO-LICENSE | Разрешено по правилу пользователя; новые сторонние зависимости, модели и шрифты не добавлялись и не обновлялись | 2026-11-06 |
| Собственный EXE-запускатель | 1.0.0 | `local_web/launcher/DocumentConverterLauncher.cs`, сборка штатным установленным C# compiler | NO-LICENSE | Разрешено для собственного кода; Python, .NET Framework, OCR-библиотеки и модели в EXE не включены | 2026-11-05 |
| Корпоративный логотип пользователя | Исходный PNG без изменений | Исходное изображение пользователя → `local_web/web/brand-logo.png` | NO-LICENSE | Разрешено по правилу пользователя для материалов без указанной лицензии; используется в утверждённом оформлении приложения | 2026-11-05 |

Системные Windows/.NET и уже установленный Python используются как среда выполнения; это не новая поставка их runtime. Автономный распространяемый комплект среды и моделей данным дополнением не разрешён и не подготовлен. Нерешённые вопросы первоначального аудита сохраняются.

## Переносимая сборка Windows x64 1.8.0, 2026-11-09

Пользователь 2026-11-09 явно разрешил включить в эту публичную готовую сборку текущее рабочее окружение из подготовленного перечня, включая Python (PSF), CUDA/cuDNN (NVIDIA EULA), certifi (MPL-2.0), Pillow (HPND), NumPy и вложенные библиотеки с лицензиями вне белого списка. Исключение относится к этой готовой сборке, а не к будущим обновлениям или другим проектам. Файлы LICENSE/NOTICE/EULA поставляемых компонентов сохраняются в их каталогах.

| Компонент | Точная версия / артефакт | Первичный источник | Лицензия / решение | Дата |
|---|---|---|---|---|
| CPython, переносимая копия текущей установки | 3.12.10 Windows x64 | https://www.python.org/downloads/release/python-31210/ ; runtime/LICENSE.txt | PSF-2.0 и включённые notices; разрешено пользователем для этой сборки | 2026-11-09 |
| Пакеты текущего OCR-окружения | 123 дистрибутива; точные версии и SHA256 файлов в portable-manifest.json | Установленные METADATA, LICENSE и NOTICE, скопированные вместе с пакетами | Существующие записи аудита и указанное выше разрешение пользователя на эту сборку | 2026-11-09 |
| llama.cpp, установленная официальная CUDA-сборка | b11811, llama-b11811-bin-win-cuda-12.4-x64.zip | https://github.com/ggml-org/llama.cpp/releases/tag/b11811 ; https://github.com/ggml-org/llama.cpp/blob/b11811/LICENSE | MIT для llama.cpp; отдельные условия CUDA покрыты разрешением пользователя на эту сборку | 2026-11-09 |

SHA256 исходного архива llama.cpp: c7857c6b20284d40e648486bcc7f342bbbc6dfcd603cc5d27bbcd9ecc3bb66d6. Совпадает с digest официального GitHub release asset. Используются прежние зафиксированные snapshots Surya 2, Heron и TableFormer; модель ориентации проверяется по SHA256 в коде. Нерелевантные Whisper/RAG-модели, документы пользователя, история, отчёты и действующие секреты в поставку не копируются.
